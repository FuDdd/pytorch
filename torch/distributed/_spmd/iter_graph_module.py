import copy
import inspect
import itertools
import logging
from contextlib import contextmanager, ExitStack
from typing import Any, Callable, cast, Dict, List, Optional, Set, Tuple, Type

import torch.nn as nn
from torch import fx
from torch.fx.graph import PythonCode
from torch.fx.node import Argument
from torch.profiler import record_function
from torch.utils._pytree import tree_flatten, tree_map, tree_unflatten


logger: logging.Logger = logging.getLogger("IterGraphModule")


class IterGraph(fx.Graph):
    """
    ``IterGraph`` is used to perform cross-iteration optimization. ``IterGraph``
    keeps track of the 3 graphs, self (the original graph), setup graph, and
    cleanup graph. The 3 graphs should be identical copies of a ``fx.Graph``.

    IterGraph subclass fx.Graph to override the necessary APIs that will be used
    when constructing a optimization, e.g., communication fusion. IterGraph also
    provides APIs that originally belong to fx.Node and all these APIs will have
    ``node_`` prefix. For example, ``IterGraph.node_prepend`` is the equivalance
    of ``fx.Node.prepend``. Note that all the optimizations must be constructed
    using these APIs.
    """

    def __init__(
        self,
        orig_graph: fx.Graph,
        setup_graph: fx.Graph,
        cleanup_graph: fx.Graph,
        owning_module: Optional[fx.GraphModule] = None,
        tracer_cls: Optional[Type["fx.Tracer"]] = None,
        tracer_extras: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(owning_module, tracer_cls, tracer_extras)

        output_vals = self.graph_copy(orig_graph, {}, return_output_node=True)
        # TODO: if we do ``deepcopy(_codegen)`` and the input argument contains
        # a dictionary with the form of Dict[torch.Tensor, Any], the
        # torch.fx._pytree.treen_flatten_spec will not be able to flatten the
        # dict -- the torch.Tensor will be duplicated because the _input_spec
        # will save the ``keys`` of a dictionary (the values are not saved).
        self._codegen = copy.deepcopy(orig_graph._codegen)
        assert isinstance(output_vals, tuple)
        output_val, old_output_val = output_vals
        super().output(output_val, type_expr=getattr(old_output_val, "type", None))

        self.setup_graph = setup_graph
        self.cleanup_graph = cleanup_graph
        self._all_graphs: Tuple[fx.Graph, ...] = (
            self.setup_graph,
            self.cleanup_graph,
            cast(fx.Graph, super()),
        )

        self._setup_mapping: Dict[fx.Node, fx.Node] = {}
        self._cleanup_mapping: Dict[fx.Node, fx.Node] = {}
        self._freeze_cross_iter_movement = False
        self._cross_iter_block_count = 0

        for node, setup_node, cleanup_node in zip(
            self.nodes, self.setup_graph.nodes, self.cleanup_graph.nodes
        ):
            self._setup_mapping[node] = setup_node
            self._cleanup_mapping[node] = cleanup_node

        self.num_extra_output = 0

    def _lookup_node(self, node: fx.Node, graph: fx.Graph) -> Optional[fx.Node]:
        if graph == self.setup_graph:
            return self._setup_mapping.get(node, None)
        elif graph == self.cleanup_graph:
            return self._cleanup_mapping.get(node, None)
        return node

    def _insert_context(self, func: str, node: fx.Node):
        with ExitStack() as stack:
            for graph in self._all_graphs:
                if node:
                    actual_node = self._lookup_node(node, graph)
                    assert actual_node is not None, "Cannot handle None case now."
                else:
                    actual_node = node
                stack.enter_context(getattr(graph, func)(actual_node))
            yield

    def _fx_graph_call(
        self, graph: fx.Graph, func: str, *args: Any, **kwargs: Any
    ) -> Any:
        fx_graph: fx.Graph = graph if graph != self else cast(fx.Graph, super())
        return getattr(fx_graph, func)(*args, **kwargs)

    @contextmanager
    def inserting_after(self, node):
        return self._insert_context("inserting_after", node)

    @contextmanager
    def inserting_before(self, node):
        return self._insert_context("inserting_before", node)

    @staticmethod
    def _find_output(graph: fx.Graph) -> fx.Node:
        for output in reversed(graph.nodes):
            if output.target == "output":
                return output

    @staticmethod
    def _is_connect_to_output(subgraph: List[fx.Node], output: fx.Node) -> bool:
        """
        This function ensure the nodes in subgraph satisfy one of the following:
        1. The user of the node is in ``subgraph``.
        2. The user of the node is output.
        3. There are no users -- the node is a side-effect node.
        """
        all_nodes: Set[fx.Node] = set(subgraph)
        for node in subgraph:
            for user in node.users:
                if not isinstance(user, fx.Node):
                    continue
                if user not in all_nodes and user != output:
                    return False
        return True

    @staticmethod
    def _clone_subgraph(
        subgraph: List[fx.Node], graph: fx.Graph, target: fx.Node
    ) -> List[fx.Node]:
        all_nodes = set(subgraph)
        mapping = dict()
        cloned_subgraph = []
        with graph.inserting_before(target):
            for node in subgraph:
                cloned_node = graph.call_function(
                    node.target, node.args, node.kwargs, node.type
                )
                # TODO: there are many flatten/unflatten in IterGraph that
                # can be simplified with tree_map. Will simplify this in
                # a follow-up PR.
                original_input, _ = tree_flatten((node.args, node.kwargs))
                cloned_input, spec = tree_flatten(
                    (cloned_node.args, cloned_node.kwargs)
                )
                mapped_cloned_input = []
                for original_input_node, cloned_input_node in zip(
                    original_input, cloned_input
                ):
                    if original_input_node in all_nodes:
                        assert original_input_node in mapping
                        mapped_cloned_input.append(mapping[original_input_node])
                    else:
                        mapped_cloned_input.append(cloned_input_node)
                cloned_node.args, cloned_node.kwargs = tree_unflatten(
                    mapped_cloned_input, spec
                )
                mapping[node] = cloned_node
                cloned_subgraph.append(cloned_node)
        return cloned_subgraph

    def _forward_subgraph_inputs(
        self, subgraph: List[fx.Node], graph: fx.Graph, erase_node: bool
    ) -> int:
        """
        This function make the inputs of a subgraph become the extra output
        of the entire graph. If ``erase_node`` is True, the subgraph will be
        erased from the graph -- essentially forward the inputs of the subgraph
        to the output of the graph.
        """
        output = self._find_output(graph)
        inputs = []
        all_nodes: Set[fx.Node] = set(subgraph)

        for node in subgraph:
            node_inputs, _ = tree_flatten((node.args, node.kwargs))
            for _input in node_inputs:
                if not isinstance(_input, fx.Node):
                    continue
                if _input in all_nodes:
                    continue
                inputs.append(_input)

        new_output = output.args + tuple(inputs)
        if erase_node:
            # We have to remove the node in the reversed order to ensure the
            # node has zero users.
            erased = set()
            for node in reversed(subgraph):
                if len(node.users) == 1:
                    key = next(iter(node.users.keys()))
                    # This is the optimizer case where IterGraph functionalize
                    # the optimizer. Remove the dependency.
                    if key not in new_output and key == output:
                        node.users.clear()
                # This is the step case where there is a virtual data dependency
                # (in-place update) between step and optimizer. And
                # functionalize_optim add this dependency
                for user in list(node.users.keys()):
                    if user in erased:
                        node.users.pop(user)
                if node.users:
                    raise RuntimeError(
                        "IterGraph has not support moving the nodes that "
                        "produce users output result."
                    )
                self._fx_graph_call(graph, "erase_node", node)
                erased.add(node)
        self._fx_graph_call(graph, "erase_node", output)
        self._fx_graph_call(graph, "output", new_output)
        logger.info(f"Extended outputs from the subgraph inputs: {inputs}")
        return len(inputs)

    def _forward_inputs_to_subgraph(
        self, subgraph: List[fx.Node], graph: fx.Graph, extra_input: int
    ) -> None:
        last_placeholder = None
        for node in graph.nodes:
            if str(node.op) != "placeholder":
                break
            last_placeholder = node
        assert last_placeholder is not None
        with self._fx_graph_call(graph, "inserting_after", last_placeholder):
            new_input_nodes = reversed(
                [
                    self._fx_graph_call(
                        graph,
                        "placeholder",
                        f"cross_iter_input_{self._cross_iter_block_count}_{i}",
                    )
                    for i in reversed(range(extra_input))
                ]
            )

        all_nodes = set(subgraph)
        try:
            for node in subgraph:
                node_inputs, spec = tree_flatten((node.args, node.kwargs))
                new_node_inputs = []
                for input_node in node_inputs:
                    if input_node in all_nodes or not isinstance(input_node, fx.Node):
                        new_node_inputs.append(input_node)
                    else:
                        new_node_inputs.append(next(new_input_nodes))
                node.args, node.kwargs = tree_unflatten(new_node_inputs, spec)
        except StopIteration:
            raise RuntimeError("There are no enough input nodes")
        try:
            unused_node = next(new_input_nodes)
            raise RuntimeError(f"There are unused nodes {unused_node}")
        except StopIteration:
            pass

    def move_to_next_iter_before(
        self, subgraph: List[fx.Node], target_node: fx.Node
    ) -> None:
        """
        Move the ``subgraph`` to the next iteration before ``target_node``.
        The ``subgraph`` is a list of fx.Node and must satisfy the following
        restrictions:
            1. The order of the nodes in ``subgraph`` must obey the topological
               sort order.
            2. The users of the node in ``subgraph`` must be one of the following:
                a.) the user is also a node in ``subgraph``.
                b.) the user is the output of the full graph.
                c.) the node has users (side effect node).
        """
        if self._freeze_cross_iter_movement:
            raise RuntimeError(
                "The cross-iteration movement has been frozen for the given "
                "IterGraph."
            )

        if not self._is_connect_to_output(subgraph, self._find_output(self)):
            raise ValueError(
                "The target nodes for ``move_to_next_iter_before`` must "
                "satisfy one of the following conditions: 1) the user of the "
                "node is in the target nodes, 2) the user is the ouput of the "
                "graph, 3) there are no users -- the node is a side-effect node. "
            )

        self._cross_iter_block_count += 1
        # The main graph must be the last one to be modified. Otherwise, the
        # mapping may change and hence intorduce incorrect mapping for setup
        # and cleanup graphs.

        # For the setup graph, no additional input is needed but additional
        # outputs will be created. The additional output represents the input of
        # the action to be moved to the next iteration -- main graph.
        setup_extra_input = self._forward_subgraph_inputs(
            subgraph=[self._lookup_node(node, self.setup_graph) for node in subgraph],
            graph=self.setup_graph,
            erase_node=True,
        )

        # For the cleanup graph, additional input is required to get the output
        # from the last iteration -- main graph. Additional nodes are also
        # needed to perform the action moved from the last itertion.
        target_cleanup_node = self._lookup_node(target_node, self.cleanup_graph)
        assert target_cleanup_node is not None, "The target_cleanup_node is None."
        cloned_subgraph = self._clone_subgraph(
            subgraph=[self._lookup_node(node, self.cleanup_graph) for node in subgraph],
            graph=self.cleanup_graph,
            target=target_cleanup_node,
        )
        self._forward_inputs_to_subgraph(
            cloned_subgraph, self.cleanup_graph, setup_extra_input
        )

        # For the main graph, additional input will be created to represent
        # the output from the last iteration -- main graph or setup graph.
        # Additional output will also be generated to represent the input for
        # the next iteration -- the main graph or the cleanup graph.
        main_extra_input = self._forward_subgraph_inputs(
            subgraph=subgraph, graph=self, erase_node=False
        )
        assert main_extra_input == setup_extra_input
        for node in subgraph:
            target_node.prepend(node)
        self._forward_inputs_to_subgraph(subgraph, self, main_extra_input)

        for node in self.cleanup_graph.nodes:
            if len(node.users) == 0:
                node.users["__hold__"] = None
        for node in self.nodes:
            if len(node.users) == 0:
                node.users["__hold__"] = None
        self.num_extra_output += main_extra_input

    def move_before(self, nodes: List[fx.Node], target_node: fx.Node) -> None:
        for graph in self._all_graphs:
            actual_nodes = [self._lookup_node(node, graph) for node in nodes]
            actual_target_node = self._lookup_node(target_node, graph)
            assert actual_target_node is not None
            for actual_node in actual_nodes:
                actual_target_node.prepend(actual_node)

    def move_after(self, nodes: List[fx.Node], target_node: fx.Node) -> None:
        for graph in self._all_graphs:
            actual_nodes = [self._lookup_node(node, graph) for node in nodes]
            actual_target_node = self._lookup_node(target_node, graph)
            for actual_node in actual_nodes:
                assert actual_target_node is not None
                actual_target_node.append(actual_node)
                actual_target_node = actual_node

    def call_function(
        self,
        the_function: Callable[..., Any],
        args: Optional[Tuple[Argument, ...]] = None,
        kwargs: Optional[Dict[str, Argument]] = None,
        type_expr: Optional[Any] = None,
    ) -> fx.Node:
        if self._freeze_cross_iter_movement:
            return super().call_function(the_function, args, kwargs, type_expr)

        setup_args = tree_map(
            lambda arg: self._lookup_node(arg, self.setup_graph)
            if isinstance(arg, fx.Node)
            else arg,
            args,
        )
        setup_kwargs = tree_map(
            lambda arg: self._lookup_node(arg, self.setup_graph)
            if isinstance(arg, fx.Node)
            else arg,
            kwargs,
        )
        cleanup_args = tree_map(
            lambda arg: self._lookup_node(arg, self.cleanup_graph)
            if isinstance(arg, fx.Node)
            else arg,
            args,
        )
        cleanup_kwargs = tree_map(
            lambda arg: self._lookup_node(arg, self.cleanup_graph)
            if isinstance(arg, fx.Node)
            else arg,
            kwargs,
        )

        setup_node = self.setup_graph.call_function(
            the_function, setup_args, setup_kwargs, type_expr
        )
        main_node = super().call_function(the_function, args, kwargs, type_expr)
        cleanup_node = self.cleanup_graph.call_function(
            the_function, cleanup_args, cleanup_kwargs, type_expr
        )
        self._setup_mapping[main_node] = setup_node
        self._cleanup_mapping[main_node] = cleanup_node
        return main_node

    def erase_node(self, to_erase: fx.Node) -> None:
        if self._freeze_cross_iter_movement:
            return super().erase_node(to_erase)

        setup_node = self._lookup_node(to_erase, self.setup_graph)
        assert setup_node is not None, "setup_node is None"
        self.setup_graph.erase_node(setup_node)
        super().erase_node(to_erase)
        cleanup_node = self._lookup_node(to_erase, self.cleanup_graph)
        self.cleanup_graph.erase_node(cleanup_node)

    def placeholder(
        self,
        name: str,
        type_expr: Optional[Any] = None,
        default_value: Any = inspect.Signature.empty,
    ) -> fx.Node:
        if self._freeze_cross_iter_movement:
            return super().placeholder(name, type_expr, default_value)

        main_placeholder = super().placeholder(name, type_expr, default_value)
        setup_placeholder = self.setup_graph.placeholder(name, type_expr, default_value)
        cleanup_placeholder = self.cleanup_graph.placeholder(
            name, type_expr, default_value
        )
        self._setup_mapping[main_placeholder] = setup_placeholder
        self._cleanup_mapping[main_placeholder] = cleanup_placeholder

    def output(self, result: Argument, type_expr: Optional[Any] = None) -> fx.Node:
        if self._freeze_cross_iter_movement:
            return super().placeholder(result, type_expr)

        main_output = super().output(result, type_expr)
        setup_result = tree_map(
            lambda _result: self._lookup_node(_result, self.setup_graph)
            if isinstance(_result, fx.Node)
            else _result,
            result,
        )
        cleanup_result = tree_map(
            lambda _result: self._lookup_node(_result, self.cleanup_graph)
            if isinstance(_result, fx.Node)
            else _result,
            result,
        )
        self.setup_graph.output(setup_result, type_expr)
        self.cleanup_graph.output(cleanup_result, type_expr)

        return main_output

    def lint(self) -> None:
        self.setup_graph.lint()
        super().lint()
        self.cleanup_graph.lint()

    def node_prepend(self, target_node: fx.Node, node: fx.Node) -> None:
        """Prepend node to target_node."""
        if self._freeze_cross_iter_movement:
            target_node.prepend(node)
            return

        for graph in self._all_graphs:
            actual_node = self._lookup_node(node, graph)
            assert actual_node is not None, "The node is None"
            actual_target_node = self._lookup_node(target_node, graph)
            assert actual_target_node is not None, "The target node is None"
            actual_target_node.prepend(actual_node)

    def node_append(self, target_node: fx.Node, node: fx.Node) -> None:
        """Append node to target_node."""
        if self._freeze_cross_iter_movement:
            target_node.append(node)
            return

        for graph in self._all_graphs:
            actual_node = self._lookup_node(node, graph)
            assert actual_node is not None, f"The actual node is None, {node}."
            actual_target_node = self._lookup_node(target_node, graph)
            assert (
                actual_target_node is not None
            ), f"The actual target node is None, {target_node}."
            actual_target_node.append(actual_node)

    def node_update_arg(self, node: fx.Node, idx: int, arg: Argument) -> None:
        if self._freeze_cross_iter_movement:
            node.update_arg(int, arg)
            return

        setup_arg = tree_map(
            lambda _arg: self._lookup_node(_arg, self.setup_graph)
            if isinstance(_arg, fx.Node)
            else _arg,
            arg,
        )
        setup_node = self._lookup_node(node, self.setup_graph)
        assert setup_node is not None, "setup_node is None"
        setup_node.update_arg(idx, setup_arg)

        node.update_arg(idx, arg)

        cleanup_arg = tree_map(
            lambda _arg: self._lookup_node(_arg, self.cleanup_graph)
            if isinstance(_arg, fx.Node)
            else _arg,
            arg,
        )
        cleanup_node = self._lookup_node(node, self.cleanup_graph)
        assert cleanup_node is not None, "cleanup_node is None"
        cleanup_node.update_arg(idx, cleanup_arg)

    def node_replace_all_uses_with(
        self,
        node: fx.Node,
        replace_with: fx.Node,
        delete_user_cb: Callable[[fx.Node], bool] = lambda user: True,
        *,
        propagate_meta=False,
    ) -> List[fx.Node]:
        for graph in self._all_graphs:
            actual_node = self._lookup_node(node, graph)
            actual_replace_with = self._lookup_node(replace_with, graph)
            assert actual_node is not None
            ret = actual_node.replace_all_uses_with(
                actual_replace_with,
                delete_user_cb,
                propagate_meta=propagate_meta,
            )
        return ret

    def node_add_user(self, node: fx.Node, user: Any) -> None:
        for graph in self._all_graphs:
            actual_node = self._lookup_node(node, graph)
            if isinstance(user, fx.Node):
                actual_user_node = self._lookup_node(user, graph)
            else:
                actual_user_node = user
            assert actual_node is not None
            actual_node.users[actual_user_node] = None  # type: ignore[index]

    def node_remove_user(self, node: fx.Node, user: Any) -> None:
        for graph in self._all_graphs:
            actual_node = self._lookup_node(node, graph)
            if isinstance(user, fx.Node):
                actual_user_node = self._lookup_node(user, graph)
            else:
                actual_user_node = user
            assert actual_node is not None
            del actual_node.users[actual_user_node]  # type: ignore[arg-type]

    def keep_unused_nodes(self) -> None:
        for node in self.nodes:
            if len(node.users) == 0 and str(node.op) != "output":
                self.node_add_user(node, "__hold__")

    def functionalize_optim(self) -> None:
        # IterGraph can only support full graph (fwd+bwd+optim). As optimizer
        # is not a functional call (it is inplace op), this mehod adds the of
        # the optimizer call. This method has strong assumption of the optimizer
        # and may not always be working. This method is intended be a temporary
        # solution only.
        for node in reversed(self.nodes):
            if node.name.startswith("output"):
                output_node = node
            elif node.name.startswith(
                "_fused_adam_",
            ):
                optim_node = node
            elif node.name.startswith(
                "_foreach_add_",
            ):
                step_node = node
                self.node_add_user(optim_node, output_node)
                self.node_add_user(step_node, optim_node)

    def defunctionalize_optim(self) -> None:
        for i, node in enumerate(reversed(self.nodes)):
            if node.name.startswith("output"):
                output_node = node
            elif node.name.startswith(
                "_fused_adam_",
            ):
                optim_node = node
            elif node.name.startswith(
                "_foreach_add_",
            ):
                step_node = node
                self.node_add_user(step_node, optim_node)
                self.node_remove_user(optim_node, output_node)
                self.node_remove_user(step_node, optim_node)

    def freeze_cross_iter_movement(self) -> None:
        self._freeze_cross_iter_movement = True


class IterGraphModule(nn.Module):
    """
    ``IterGraphModule`` provides the ability to do cross-iteration optimization.
    Given a ``fx.GraphModule``, main_gm, ``IterGraphModule`` internally
    duplicate it to 3 copies and redirect the ``forward`` request to a different
    ``fx.GraphModule`` based on the iteration count. This allows users to do
    graph optimizations that across iterations (e.g., moving collective wait in
    the backward to the forward of the next iteration).

    Note that users must call the APIs provided by ``IterGraphModule`` or
    ``IterGraph`` to rewrite the graph so that ``IterGraphModule`` can keep the
    data dependency for all 3 graphs.
    """

    def __init__(self, main_gm: fx.GraphModule) -> None:
        super().__init__()

        def _copy_gm(src: fx.GraphModule, graph: fx.Graph) -> fx.GraphModule:
            gm = fx.GraphModule(src, graph)
            gm.meta = getattr(graph, "meta", {})
            return gm

        self.setup_gm = _copy_gm(main_gm, copy.deepcopy(main_gm.graph))
        self.cleanup_gm = _copy_gm(main_gm, copy.deepcopy(main_gm.graph))
        self.main_gm = _copy_gm(
            main_gm,
            IterGraph(main_gm.graph, self.setup_gm.graph, self.cleanup_gm.graph),
        )

        self._iter = 0
        self._max_iters = 0
        self._previous_output: Tuple[Any, ...] = tuple()

    def setup(self, max_iters: int = 0) -> None:
        """
        This method is used to tell IterGraphModule the iterations to train so
        that IterGraphModule knows which iteration is the last one and can do
        proper cleanup.
        """
        # TODO: There are cases where max_iters is not known or not precise,
        # e.g., data is depleted. One suggestion from the reviewer is to
        # add one extra argument to forward(..., last_iter: bool = False) to
        # allow users to tell us if the last iteration happens.
        if max_iters <= 0:
            raise ValueError(f"Incorrect max_iters is set, {max_iters}")
        self._iter = 0
        self._max_iters = max_iters

    def _run(self, gm: fx.GraphModule, *args, **kwargs) -> Any:
        if cast(IterGraph, self.main_gm.graph).num_extra_output > 0:
            # TODO: a general way to support different types of input and output.
            assert not kwargs, "Has not supported kwargs now."
            new_args = args + (self._previous_output)
            output = gm(*new_args, **kwargs)
            if self._iter < self._max_iters:
                assert isinstance(
                    output, tuple
                ), f"Only support tuple output now. {type(output)}"
                num_actual_output = (
                    len(output) - cast(IterGraph, self.main_gm.graph).num_extra_output
                )
                assert num_actual_output > 0
                self._previous_output = output[num_actual_output:]
                output = output[:num_actual_output]
                if len(output) == 1:
                    output = output[0]
        else:
            # No cross-iteration optimization is done. Simply call the
            # GraphModule.
            output = gm(*args, **kwargs)
        logger.debug(f"The output information: size={len(output)}, type={type(output)}")
        return output

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        self._iter += 1
        if self._iter == 1:
            logger.info("Using the setup graph")
            gm = self.setup_gm
            profiler_string = "## IterGraphModule: Setup Graph ##"
        elif self._iter == self._max_iters:
            logger.info("Using the cleanup graph")
            gm = self.cleanup_gm
            profiler_string = "## IterGraphModule: Cleanup Graph ##"
        else:
            gm = self.main_gm
            if self._iter == 2:
                logger.info("Using the main graph")
                profiler_string = "## IterGraphModule -- Maybe Compiling ##"
            else:
                profiler_string = "## IterGraphModule ##"

        with record_function(profiler_string):
            return self._run(gm, *args, **kwargs)

    @property
    def graph(self) -> IterGraph:
        return cast(IterGraph, self.main_gm.graph)

    def recompile(self) -> PythonCode:
        self.setup_gm.recompile()
        self.cleanup_gm.recompile()
        return self.main_gm.recompile()

    def print_readable(self, print_output: bool = True) -> str:
        return self.main_gm.print_readable(print_output)

    def print_all_graphs(self) -> None:
        logger.info("Printing the three fx.Graph:")
        logger.info("1. Setup fx.Graph:")
        logger.info(f"{self.setup_gm.graph}")
        logger.info("2. Main fx.Graph:")
        logger.info(f"{self.main_gm.graph}")
        logger.info("3. Cleanup fx.Graph:")
        logger.info(f"{self.cleanup_gm.graph}")

    def print_all_graph_modules(self) -> None:
        logger.info("Printing the three fx gm:")
        logger.info("1. Setup fx.GraphModule:")
        logger.info(f"{self.setup_gm.print_readable(False)}")
        logger.info("2. Main fx.GraphModule:")
        logger.info(f"{self.main_gm.print_readable(False)}")
        logger.info("3. Cleanup fx.GraphModule:")
        logger.info(f"{self.cleanup_gm.print_readable(False)}")
