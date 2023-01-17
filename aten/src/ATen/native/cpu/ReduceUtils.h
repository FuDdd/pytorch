#pragma once

#include <ATen/Parallel.h>
#include <ATen/NumericUtils.h>
#include <ATen/cpu/vec/vec.h>
#include <ATen/cpu/vec/functional.h>
#include <ATen/native/ReductionType.h>
#include <c10/util/irange.h>

namespace at::native {
inline namespace CPU_CAPABILITY {

using namespace vec;

#define AT_DISPATCH_REDUCTION_TYPES(op, ...)                                   \
  [&] {                                                                        \
    switch (op) {                                                              \
      case SUM: {                                                              \
        static constexpr ReductionType reduce = SUM;                           \
        return __VA_ARGS__();                                                  \
      }                                                                        \
      case MEAN: {                                                             \
        static constexpr ReductionType reduce = MEAN;                          \
        return __VA_ARGS__();                                                  \
      }                                                                        \
      case MIN: {                                                              \
        static constexpr ReductionType reduce = MIN;                           \
        return __VA_ARGS__();                                                  \
      }                                                                        \
      case MAX: {                                                              \
        static constexpr ReductionType reduce = MAX;                           \
        return __VA_ARGS__();                                                  \
      }                                                                        \
      case PROD: {                                                             \
        static constexpr ReductionType reduce = PROD;                          \
        return __VA_ARGS__();                                                  \
      }                                                                        \
    }                                                                          \
  }()

template <typename scalar_t, ReductionType reduce>
inline vec_scalar_t<scalar_t> init_value() {
  using acc_t = vec_scalar_t<scalar_t>;
  acc_t val;
  if (reduce == ReductionType::SUM ||
      reduce == ReductionType::MEAN) {
    val = static_cast<acc_t>(0);
  } else if (reduce == ReductionType::PROD) {
    val = static_cast<acc_t>(1);
  } else if (reduce == ReductionType::MAX) {
    val = -std::numeric_limits<acc_t>::infinity();
  } else {
    val = std::numeric_limits<acc_t>::infinity();
  }
  return val;
}

template <typename scalar_t, ReductionType reduce>
inline vec_scalar_t<scalar_t> init_value(const c10::optional<Scalar>& initial) {
  using acc_t = vec_scalar_t<scalar_t>;
  if (initial.has_value()) {
    return initial.value().to<acc_t>();
  } else {
    return init_value<scalar_t, reduce>();
  }
}

template <typename scalar_t>
inline void init(scalar_t* out, int64_t size, const vec_scalar_t<scalar_t>& val) {
  using Vec = Vectorized<vec_scalar_t<scalar_t>>;
  map<scalar_t>(
      [val](Vec x) { return Vec(val); },
      out,
      out,
      size);
}

template <typename scalar_t, ReductionType reduce>
inline void init(scalar_t* out, int64_t size, const c10::optional<Scalar>& initial) {
  using acc_t = vec_scalar_t<scalar_t>;
  acc_t val = init_value<scalar_t, reduce>(initial);
  init(out, size, val);
}

// overload with `include_self`, used by scatter_reduce
template <typename scalar_t, ReductionType reduce>
inline void init(scalar_t* out, int64_t size, bool include_self) {
  using acc_t = vec_scalar_t<scalar_t>;
  if (!include_self) {
    acc_t val = init_value<scalar_t, reduce>();
    init(out, size, val);
  }
}

// for Max and Min, propagate NaN:
template <typename Vec, ReductionType reduce>
inline Vec update(const Vec& x, const Vec& y) {
  if (reduce == ReductionType::SUM ||
      reduce == ReductionType::MEAN) {
    return x + y;
  } else if (reduce == ReductionType::PROD) {
    return x * y;
  } else if (reduce == ReductionType::MAX) {
    // true = all ones, false = all zeros
    Vec mask = (y > x) | y.isnan();
    return Vec::blendv(x, y, mask);
  } else {
    // true = all ones, false = all zeros
    Vec mask = (y < x) | y.isnan();
    return Vec::blendv(x, y, mask);
  }
}

template <typename scalar_t, ReductionType reduce>
inline void update(scalar_t* out, scalar_t* data, int64_t K) {
  using Vec = vec::Vectorized<vec_scalar_t<scalar_t>>;
  map2<scalar_t>(
      [](Vec x, Vec y) { return update<Vec, reduce>(x, y); },
      out,
      out,
      data,
      K);
}

template <typename scalar_t, ReductionType reduce>
inline void write(scalar_t* out, int64_t count, int64_t K) {
  using Vec = vec::Vectorized<vec_scalar_t<scalar_t>>;
  if (reduce == ReductionType::MEAN) {
    if (count > 0) {
      vec::map<scalar_t>(
          [count](Vec x) { return x / Vec(count); },
          out,
          out,
          K);
    }
  }
}

} // namespace CPU_CAPABILITY
} // namespace at::native
