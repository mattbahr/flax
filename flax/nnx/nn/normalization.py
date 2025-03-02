# Copyright 2024 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import typing as tp

import jax
import jax.numpy as jnp
from jax import lax

from flax import nnx
from flax.nnx import rnglib
from flax.nnx.module import Module, first_from
from flax.nnx.nn import dtypes, initializers
from flax.typing import (
  Array,
  Dtype,
  Initializer,
  Axes,
)


def _canonicalize_axes(rank: int, axes: Axes) -> tp.Tuple[int, ...]:
  """Returns a tuple of deduplicated, sorted, and positive axes."""
  if not isinstance(axes, tp.Iterable):
    axes = (axes,)
  return tuple({rank + axis if axis < 0 else axis for axis in axes})


def _abs_sq(x):
  """Computes the elementwise square of the absolute value |x|^2."""
  if jnp.iscomplexobj(x):
    return lax.square(lax.real(x)) + lax.square(lax.imag(x))
  else:
    return lax.square(x)


def _compute_stats(
  x: Array,
  axes: Axes,
  dtype: tp.Optional[Dtype],
  axis_name: tp.Optional[str] = None,
  axis_index_groups: tp.Any = None,
  use_mean: bool = True,
  use_fast_variance: bool = True,
  mask: tp.Optional[Array] = None,
):
  """Computes mean and variance statistics.

  This implementation takes care of a few important details:
  - Computes in float32 precision for stability in half precision training.
  - If ``use_fast_variance`` is ``True``, mean and variance are computed using
    Var = E[|x|^2] - |E[x]|^2, instead of Var = E[|x - E[x]|^2]), in a single
    XLA fusion.
  - Clips negative variances to zero which can happen due to
    roundoff errors. This avoids downstream NaNs.
  - Supports averaging across a parallel axis and subgroups of a parallel axis
    with a single ``lax.pmean`` call to avoid latency.

  Arguments:
    x: Input array.
    axes: The axes in ``x`` to compute mean and variance statistics for.
    dtype: Optional dtype specifying the minimal precision. Statistics are
      always at least float32 for stability (default: dtype of x).
    axis_name: Optional name for the pmapped axis to compute mean over. Note,
      this is only used for pmap and shard map. For SPMD jit, you do not need to
      manually synchronize. Just make sure that the axes are correctly annotated
      and XLA:SPMD will insert the necessary collectives.
    axis_index_groups: Optional axis indices.
    use_mean: If true, calculate the mean from the input and use it when
      computing the variance. If false, set the mean to zero and compute the
      variance without subtracting the mean.
    use_fast_variance: If true, use a faster, but less numerically stable,
      calculation for the variance.
    mask: Binary array of shape broadcastable to ``inputs`` tensor, indicating
      the positions for which the mean and variance should be computed.

  Returns:
    A pair ``(mean, var)``.
  """
  if dtype is None:
    dtype = jnp.result_type(x)
  # promote x to at least float32, this avoids half precision computation
  # but preserves double or complex floating points
  dtype = jnp.promote_types(dtype, jnp.float32)
  x = jnp.asarray(x, dtype)
  axes = _canonicalize_axes(x.ndim, axes)

  def maybe_distributed_mean(*xs, mask=None):
    mus = tuple(x.mean(axes, where=mask) for x in xs)
    if axis_name is None:
      return mus if len(xs) > 1 else mus[0]
    else:
      # In the distributed case we stack multiple arrays to speed comms.
      if len(xs) > 1:
        reduced_mus = lax.pmean(
          jnp.stack(mus, axis=0),
          axis_name,
          axis_index_groups=axis_index_groups,
        )
        return tuple(reduced_mus[i] for i in range(len(xs)))
      else:
        return lax.pmean(mus[0], axis_name, axis_index_groups=axis_index_groups)

  if use_mean:
    if use_fast_variance:
      mu, mu2 = maybe_distributed_mean(x, _abs_sq(x), mask=mask)
      # mean2 - _abs_sq(mean) is not guaranteed to be non-negative due
      # to floating point round-off errors.
      var = jnp.maximum(0.0, mu2 - _abs_sq(mu))
    else:
      mu = maybe_distributed_mean(x, mask=mask)
      var = maybe_distributed_mean(
        _abs_sq(x - jnp.expand_dims(mu, axes)), mask=mask
      )
  else:
    var = maybe_distributed_mean(_abs_sq(x), mask=mask)
    mu = jnp.zeros_like(var)
  return mu, var


def _normalize(
  x: Array,
  mean: Array,
  var: Array,
  scale: tp.Optional[Array],
  bias: tp.Optional[Array],
  reduction_axes: Axes,
  feature_axes: Axes,
  dtype: tp.Optional[Dtype],
  epsilon: float,
):
  """ "Normalizes the input of a normalization layer and optionally applies a learned scale and bias.

  Arguments:
    x: The input.
    mean: Mean to use for normalization.
    var: Variance to use for normalization.
    reduction_axes: The axes in ``x`` to reduce.
    feature_axes: Axes containing features. A separate bias and scale is learned
      for each specified feature.
    dtype: The dtype of the result (default: infer from input and params).
    epsilon: Normalization epsilon.

  Returns:
    The normalized input.
  """
  reduction_axes = _canonicalize_axes(x.ndim, reduction_axes)
  feature_axes = _canonicalize_axes(x.ndim, feature_axes)
  stats_shape = list(x.shape)
  for axis in reduction_axes:
    stats_shape[axis] = 1
  mean = mean.reshape(stats_shape)
  var = var.reshape(stats_shape)
  feature_shape = [1] * x.ndim
  for ax in feature_axes:
    feature_shape[ax] = x.shape[ax]
  y = x - mean
  mul = lax.rsqrt(var + epsilon)
  args = [x]
  if scale is not None:
    scale = scale.reshape(feature_shape)
    mul *= scale
    args.append(scale)
  y *= mul
  if bias is not None:
    bias = bias.reshape(feature_shape)
    y += bias
    args.append(bias)
  dtype = dtypes.canonicalize_dtype(*args, dtype=dtype)
  return jnp.asarray(y, dtype)


def _l2_normalize(x, axis=None, eps=1e-12):
  """Normalizes along dimension `axis` using an L2 norm.

  This specialized function exists for numerical stability reasons.

  Args:
    x: An input ndarray.
    axis: Dimension along which to normalize, e.g. `1` to separately normalize
      vectors in a batch. Passing `None` views `t` as a flattened vector when
      calculating the norm (equivalent to Frobenius norm).
    eps: Epsilon to avoid dividing by zero.

  Returns:
    An array of the same shape as 'x' L2-normalized along 'axis'.
  """
  return x * lax.rsqrt((x * x).sum(axis=axis, keepdims=True) + eps)


class BatchNorm(Module):
  """BatchNorm Module.

  To calculate the batch norm on the input and update the batch statistics,
  call the :func:`train` method (or pass in ``use_running_average=False`` in
  the constructor or during call time).

  To use the stored batch statistics' running average, call the :func:`eval`
  method (or pass in ``use_running_average=True`` in the constructor or
  during call time).

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> x = jax.random.normal(jax.random.key(0), (5, 6))
    >>> layer = nnx.BatchNorm(num_features=6, momentum=0.9, epsilon=1e-5,
    ...                       dtype=jnp.float32, rngs=nnx.Rngs(0))
    >>> jax.tree.map(jnp.shape, nnx.state(layer))
    State({
      'bias': VariableState(
        type=Param,
        value=(6,)
      ),
      'mean': VariableState(
        type=BatchStat,
        value=(6,)
      ),
      'scale': VariableState(
        type=Param,
        value=(6,)
      ),
      'var': VariableState(
        type=BatchStat,
        value=(6,)
      )
    })

    >>> # calculate batch norm on input and update batch statistics
    >>> layer.train()
    >>> y = layer(x)
    >>> batch_stats1 = nnx.state(layer, nnx.BatchStat)
    >>> y = layer(x)
    >>> batch_stats2 = nnx.state(layer, nnx.BatchStat)
    >>> assert (batch_stats1['mean'].value != batch_stats2['mean'].value).all()
    >>> assert (batch_stats1['var'].value != batch_stats2['var'].value).all()

    >>> # use stored batch statistics' running average
    >>> layer.eval()
    >>> y = layer(x)
    >>> batch_stats3 = nnx.state(layer, nnx.BatchStat)
    >>> assert (batch_stats2['mean'].value == batch_stats3['mean'].value).all()
    >>> assert (batch_stats2['var'].value == batch_stats3['var'].value).all()

  Args:
    num_features: the number of input features.
    use_running_average: if True, the stored batch statistics will be
      used instead of computing the batch statistics on the input.
    axis: the feature or non-batch axis of the input.
    momentum: decay rate for the exponential moving average of
      the batch statistics.
    epsilon: a small float added to variance to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    use_bias:  if True, bias (beta) is added.
    use_scale: if True, multiply by scale (gamma).
      When the next layer is linear (also e.g. nn.relu), this can be disabled
      since the scaling will be done by the next layer.
    bias_init: initializer for bias, by default, zero.
    scale_init: initializer for scale, by default, one.
    axis_name: the axis name used to combine batch statistics from multiple
      devices. See ``jax.pmap`` for a description of axis names (default: None).
    axis_index_groups: groups of axis indices within that named axis
      representing subsets of devices to reduce over (default: None). For
      example, ``[[0, 1], [2, 3]]`` would independently batch-normalize over
      the examples on the first two and last two devices. See ``jax.lax.psum``
      for more details.
    use_fast_variance: If true, use a faster, but less numerically stable,
      calculation for the variance.
    rngs: rng key.
  """

  def __init__(
    self,
    num_features: int,
    *,
    use_running_average: bool = False,
    axis: int = -1,
    momentum: float = 0.99,
    epsilon: float = 1e-5,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_bias: bool = True,
    use_scale: bool = True,
    bias_init: Initializer = initializers.zeros_init(),
    scale_init: Initializer = initializers.ones_init(),
    axis_name: tp.Optional[str] = None,
    axis_index_groups: tp.Any = None,
    use_fast_variance: bool = True,
    rngs: rnglib.Rngs,
  ):
    feature_shape = (num_features,)
    self.mean = nnx.BatchStat(jnp.zeros(feature_shape, jnp.float32))
    self.var = nnx.BatchStat(jnp.ones(feature_shape, jnp.float32))

    self.scale: nnx.Param[jax.Array] | None
    if use_scale:
      key = rngs.params()
      self.scale = nnx.Param(scale_init(key, feature_shape, param_dtype))
    else:
      self.scale = None

    self.bias: nnx.Param[jax.Array] | None
    if use_bias:
      key = rngs.params()
      self.bias = nnx.Param(bias_init(key, feature_shape, param_dtype))
    else:
      self.bias = None

    self.num_features = num_features
    self.use_running_average = use_running_average
    self.axis = axis
    self.momentum = momentum
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_bias = use_bias
    self.use_scale = use_scale
    self.bias_init = bias_init
    self.scale_init = scale_init
    self.axis_name = axis_name
    self.axis_index_groups = axis_index_groups
    self.use_fast_variance = use_fast_variance

  def __call__(
    self,
    x,
    use_running_average: tp.Optional[bool] = None,
    *,
    mask: tp.Optional[jax.Array] = None,
  ):
    """Normalizes the input using batch statistics.

    Args:
      x: the input to be normalized.
      use_running_average: if true, the stored batch statistics will be
        used instead of computing the batch statistics on the input. The
        ``use_running_average`` flag passed into the call method will take
        precedence over the ``use_running_average`` flag passed into the
        constructor.

    Returns:
      Normalized inputs (the same shape as inputs).
    """

    use_running_average = first_from(
      use_running_average,
      self.use_running_average,
      error_msg="""No `use_running_average` argument was provided to BatchNorm
        as either a __call__ argument, class attribute, or nnx.flag.""",
    )
    feature_axes = _canonicalize_axes(x.ndim, self.axis)
    reduction_axes = tuple(i for i in range(x.ndim) if i not in feature_axes)

    if use_running_average:
      mean, var = self.mean.value, self.var.value
    else:
      mean, var = _compute_stats(
        x,
        reduction_axes,
        dtype=self.dtype,
        axis_name=self.axis_name,
        axis_index_groups=self.axis_index_groups,
        use_fast_variance=self.use_fast_variance,
        mask=mask,
      )

      self.mean.value = (
        self.momentum * self.mean.value + (1 - self.momentum) * mean
      )
      self.var.value = (
        self.momentum * self.var.value + (1 - self.momentum) * var
      )

    return _normalize(
      x,
      mean,
      var,
      self.scale.value if self.scale else None,
      self.bias.value if self.bias else None,
      reduction_axes,
      feature_axes,
      self.dtype,
      self.epsilon,
    )


class LayerNorm(Module):
  """Layer normalization (https://arxiv.org/abs/1607.06450).

  LayerNorm normalizes the activations of the layer for each given example in a
  batch independently, rather than across a batch like Batch Normalization.
  i.e. applies a transformation that maintains the mean activation within
  each example close to 0 and the activation standard deviation close to 1.

  Example usage::

    >>> from flax import nnx
    >>> import jax

    >>> x = jax.random.normal(jax.random.key(0), (3, 4, 5, 6))
    >>> layer = nnx.LayerNorm(num_features=6, rngs=nnx.Rngs(0))

    >>> nnx.state(layer)
    State({
      'bias': VariableState( # 6 (24 B)
        type=Param,
        value=Array([0., 0., 0., 0., 0., 0.], dtype=float32)
      ),
      'scale': VariableState( # 6 (24 B)
        type=Param,
        value=Array([1., 1., 1., 1., 1., 1.], dtype=float32)
      )
    })

    >>> y = layer(x)

  Args:
    num_features: the number of input features.
    epsilon: A small float added to variance to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    use_bias:  If True, bias (beta) is added.
    use_scale: If True, multiply by scale (gamma). When the next layer is linear
        (also e.g. nnx.relu), this can be disabled since the scaling will be done
        by the next layer.
    bias_init: Initializer for bias, by default, zero.
    scale_init: Initializer for scale, by default, one.
    reduction_axes: Axes for computing normalization statistics.
    feature_axes: Feature axes for learned bias and scaling.
    axis_name: the axis name used to combine batch statistics from multiple
        devices. See ``jax.pmap`` for a description of axis names (default: None).
        This is only needed if the model is subdivided across devices, i.e. the
        array being normalized is sharded across devices within a pmap.
    axis_index_groups: groups of axis indices within that named axis
        representing subsets of devices to reduce over (default: None). For
        example, ``[[0, 1], [2, 3]]`` would independently batch-normalize over
        the examples on the first two and last two devices. See ``jax.lax.psum``
        for more details.
    use_fast_variance: If true, use a faster, but less numerically stable,
        calculation for the variance.
    rngs: rng key.
  """

  def __init__(
    self,
    num_features: int,
    *,
    epsilon: float = 1e-6,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_bias: bool = True,
    use_scale: bool = True,
    bias_init: Initializer = initializers.zeros_init(),
    scale_init: Initializer = initializers.ones_init(),
    reduction_axes: Axes = -1,
    feature_axes: Axes = -1,
    axis_name: tp.Optional[str] = None,
    axis_index_groups: tp.Any = None,
    use_fast_variance: bool = True,
    rngs: rnglib.Rngs,
  ):
    feature_shape = (num_features,)

    self.scale: nnx.Param[jax.Array] | None
    if use_scale:
      key = rngs.params()
      self.scale = nnx.Param(scale_init(key, feature_shape, param_dtype))
    else:
      self.scale = None

    self.bias: nnx.Param[jax.Array] | None
    if use_bias:
      key = rngs.params()
      self.bias = nnx.Param(bias_init(key, feature_shape, param_dtype))
    else:
      self.bias = None

    self.num_features = num_features
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_bias = use_bias
    self.use_scale = use_scale
    self.bias_init = bias_init
    self.scale_init = scale_init
    self.reduction_axes = reduction_axes
    self.feature_axes = feature_axes
    self.axis_name = axis_name
    self.axis_index_groups = axis_index_groups
    self.use_fast_variance = use_fast_variance

  def __call__(self, x, *, mask: tp.Optional[jax.Array] = None):
    """Applies layer normalization on the input.

    Args:
      x: the inputs

    Returns:
      Normalized inputs (the same shape as inputs).
    """
    mean, var = _compute_stats(
      x,
      self.reduction_axes,
      self.dtype,
      self.axis_name,
      self.axis_index_groups,
      use_fast_variance=self.use_fast_variance,
      mask=mask,
    )

    return _normalize(
      x,
      mean,
      var,
      self.scale.value if self.scale else None,
      self.bias.value if self.bias else None,
      self.reduction_axes,
      self.feature_axes,
      self.dtype,
      self.epsilon,
    )


class RMSNorm(Module):
  """RMS Layer normalization (https://arxiv.org/abs/1910.07467).

  RMSNorm normalizes the activations of the layer for each given example in a
  batch independently, rather than across a batch like Batch Normalization.
  Unlike LayerNorm which re-centers the mean to be 0 and normalizes by the
  standard deviation of the activations, RMSNorm does not re-center at all
  and instead normalizes by the root mean square of the activations.

  Example usage::

    >>> from flax import nnx
    >>> import jax

    >>> x = jax.random.normal(jax.random.key(0), (5, 6))
    >>> layer = nnx.RMSNorm(num_features=6, rngs=nnx.Rngs(0))

    >>> nnx.state(layer)
    State({
      'scale': VariableState( # 6 (24 B)
        type=Param,
        value=Array([1., 1., 1., 1., 1., 1.], dtype=float32)
      )
    })

    >>> y = layer(x)

  Args:
    num_features: the number of input features.
    epsilon: A small float added to variance to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    use_scale: If True, multiply by scale (gamma). When the next layer is linear
        (also e.g. nn.relu), this can be disabled since the scaling will be done
        by the next layer.
    scale_init: Initializer for scale, by default, one.
    reduction_axes: Axes for computing normalization statistics.
    feature_axes: Feature axes for learned bias and scaling.
    axis_name: the axis name used to combine batch statistics from multiple
        devices. See ``jax.pmap`` for a description of axis names (default: None).
        This is only needed if the model is subdivided across devices, i.e. the
        array being normalized is sharded across devices within a pmap.
    axis_index_groups: groups of axis indices within that named axis
        representing subsets of devices to reduce over (default: None). For
        example, ``[[0, 1], [2, 3]]`` would independently batch-normalize over
        the examples on the first two and last two devices. See ``jax.lax.psum``
        for more details.
    use_fast_variance: If true, use a faster, but less numerically stable,
        calculation for the variance.
    rngs: rng key.
  """

  def __init__(
    self,
    num_features: int,
    *,
    epsilon: float = 1e-6,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_scale: bool = True,
    scale_init: Initializer = initializers.ones,
    reduction_axes: Axes = -1,
    feature_axes: Axes = -1,
    axis_name: tp.Optional[str] = None,
    axis_index_groups: tp.Any = None,
    use_fast_variance: bool = True,
    rngs: rnglib.Rngs,
  ):
    feature_shape = (num_features,)

    self.scale: nnx.Param[jax.Array] | None
    if use_scale:
      key = rngs.params()
      self.scale = nnx.Param(scale_init(key, feature_shape, param_dtype))
    else:
      self.scale = None

    self.num_features = num_features
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_scale = use_scale
    self.scale_init = scale_init
    self.reduction_axes = reduction_axes
    self.feature_axes = feature_axes
    self.axis_name = axis_name
    self.axis_index_groups = axis_index_groups
    self.use_fast_variance = use_fast_variance

  def __call__(self, x, mask: tp.Optional[jax.Array] = None):
    """Applies layer normalization on the input.

    Args:
      x: the inputs

    Returns:
      Normalized inputs (the same shape as inputs).
    """
    mean, var = _compute_stats(
      x,
      self.reduction_axes,
      self.dtype,
      self.axis_name,
      self.axis_index_groups,
      use_mean=False,
      use_fast_variance=self.use_fast_variance,
      mask=mask,
    )

    return _normalize(
      x,
      mean,
      var,
      self.scale.value if self.scale else None,
      None,
      self.reduction_axes,
      self.feature_axes,
      self.dtype,
      self.epsilon,
    )

class GroupNorm(Module):
  """Group normalization (arxiv.org/abs/1803.08494).

  This op is similar to batch normalization, but statistics are shared across
  equally-sized groups of channels and not shared across batch dimension.
  Thus, group normalization does not depend on the batch composition and does
  not require maintaining internal state for storing statistics.
  The user should either specify the total number of channel groups or the
  number of channels per group.

  .. note::
    LayerNorm is a special case of GroupNorm where ``num_groups=1``.

  Example usage::

    >>> from flax import nnx
    >>> import jax
    >>> import numpy as np
    ...
    >>> x = jax.random.normal(jax.random.key(0), (3, 4, 5, 6))
    >>> layer = nnx.GroupNorm(num_features=6, num_groups=3, rngs=nnx.Rngs(0))
    >>> nnx.state(layer)
    State({
      'bias': VariableState( # 6 (24 B)
        type=Param,
        value=Array([0., 0., 0., 0., 0., 0.], dtype=float32)
      ),
      'scale': VariableState( # 6 (24 B)
        type=Param,
        value=Array([1., 1., 1., 1., 1., 1.], dtype=float32)
      )
    })
    >>> y = layer(x)
    ...
    >>> y = nnx.GroupNorm(num_features=6, num_groups=1, rngs=nnx.Rngs(0))(x)
    >>> y2 = nnx.LayerNorm(num_features=6, reduction_axes=(1, 2, 3), rngs=nnx.Rngs(0))(x)
    >>> np.testing.assert_allclose(y, y2)

  Args:
    num_features: the number of input features/channels.
    num_groups: the total number of channel groups. The default value of 32 is
      proposed by the original group normalization paper.
    group_size: the number of channels in a group.
    epsilon: A small float added to variance to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    use_bias:  If True, bias (beta) is added.
    use_scale: If True, multiply by scale (gamma). When the next layer is linear
      (also e.g. nn.relu), this can be disabled since the scaling will be done
      by the next layer.
    bias_init: Initializer for bias, by default, zero.
    scale_init: Initializer for scale, by default, one.
    reduction_axes: List of axes used for computing normalization statistics.
      This list must include the final dimension, which is assumed to be the
      feature axis. Furthermore, if the input used at call time has additional
      leading axes compared to the data used for initialisation, for example due
      to batching, then the reduction axes need to be defined explicitly.
    axis_name: the axis name used to combine batch statistics from multiple
      devices. See ``jax.pmap`` for a description of axis names (default: None).
      This is only needed if the model is subdivided across devices, i.e. the
      array being normalized is sharded across devices within a pmap or shard
      map. For SPMD jit, you do not need to manually synchronize. Just make sure
      that the axes are correctly annotated and XLA:SPMD will insert the
      necessary collectives.
    axis_index_groups: groups of axis indices within that named axis
      representing subsets of devices to reduce over (default: None). For
      example, ``[[0, 1], [2, 3]]`` would independently batch-normalize over the
      examples on the first two and last two devices. See ``jax.lax.psum`` for
      more details.
    use_fast_variance: If true, use a faster, but less numerically stable,
      calculation for the variance.
    rngs: rng key.
  """

  def __init__(
    self,
    num_features: int,
    num_groups: tp.Optional[int] = 32,
    group_size: tp.Optional[int] = None,
    *,
    epsilon: float = 1e-6,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_bias: bool = True,
    use_scale: bool = True,
    bias_init: Initializer = initializers.zeros_init(),
    scale_init: Initializer = initializers.ones_init(),
    reduction_axes: tp.Optional[Axes] = None,
    axis_name: tp.Optional[str] = None,
    axis_index_groups: tp.Any = None,
    use_fast_variance: bool = True,
    rngs: rnglib.Rngs,
  ):
    self.feature_axis = -1

    if (num_groups is None and group_size is None) or (
      num_groups is not None and group_size is not None
    ):
      raise ValueError(
        'Either `num_groups` or `group_size` should be '
        'specified. If `group_size` is to be specified, '
        'pass `num_groups=None` as argument to override '
        'the default `num_groups` value of 32.'
      )

    if group_size is not None:
      if num_features % group_size != 0:
        raise ValueError(
          'Number of features ({}) is not multiple of the '
          'group size ({}).'.format(num_features, group_size)
        )
      self.num_groups = num_features // group_size
      self.group_size = group_size
    else:
      if not isinstance(num_groups, int) or num_groups <= 0 or (
        num_features % num_groups != 0
      ):
        raise ValueError(
          'Number of groups ({}) does not divide the number'
          ' of channels ({}).'.format(num_groups, num_features)
        )
      self.num_groups = num_groups
      self.group_size = num_features // num_groups

    feature_shape = (num_features,)
    self.scale: nnx.Param[jax.Array] | None
    if use_scale:
      key = rngs.params()
      self.scale = nnx.Param(scale_init(key, feature_shape, param_dtype))
    else:
      self.scale = None

    self.bias: nnx.Param[jax.Array] | None
    if use_bias:
      key = rngs.params()
      self.bias = nnx.Param(bias_init(key, feature_shape, param_dtype))
    else:
      self.bias = None

    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_bias = use_bias
    self.use_scale = use_scale
    self.bias_init = bias_init
    self.scale_init = scale_init
    self.reduction_axes = reduction_axes
    self.axis_name = axis_name
    self.axis_index_groups = axis_index_groups
    self.use_fast_variance = use_fast_variance

  def __call__(self, x, *, mask: tp.Optional[jax.Array] = None):
    """Applies group normalization to the input (arxiv.org/abs/1803.08494).

    Args:
      x: the input of shape ``...self.num_features`` where ``self.num_features``
        is a channels dimension and ``...`` represents an arbitrary number of
        extra dimensions that can be used to accumulate statistics over. If no
        reduction axes have been specified then all additional dimensions ``...``
        will be used to accumulate statistics apart from the leading dimension
        which is assumed to represent the batch.
      mask: Binary array of shape broadcastable to ``inputs`` tensor, indicating
        the positions for which the mean and variance should be computed.

    Returns:
      Normalized inputs (the same shape as inputs).
    """
    if self.reduction_axes is not None:
      reduction_axes = self.reduction_axes
    else:
      reduction_axes = list(range(1, x.ndim - 1)) + [-1]
    reduction_axes = _canonicalize_axes(x.ndim, reduction_axes)

    group_shape = x.shape[:-1] + (self.num_groups, self.group_size)
    if mask is not None:
      mask = mask.reshape(mask.shape[:-1] + (self.num_groups, self.group_size))

    mean, var = _compute_stats(
      x.reshape(group_shape),
      list(reduction_axes[:-1]) + [-1],
      self.dtype,
      self.axis_name,
      self.axis_index_groups,
      use_fast_variance=self.use_fast_variance,
      mask=mask,
    )
    mean = jnp.repeat(mean, self.group_size, axis=1)
    var = jnp.repeat(var, self.group_size, axis=1)
    return _normalize(
      x,
      mean,
      var,
      self.scale.value if self.scale else None,
      self.bias.value if self.bias else None,
      reduction_axes[:-1],
      (self.feature_axis,),
      self.dtype,
      self.epsilon,
    )


class WeightNorm(Module):
  """L2 weight normalization (https://arxiv.org/abs/1602.07868).
  Weight normalization normalizes the weight params so that the l2-norm of
  the matrix is equal to 1. This is implemented as a layer wrapper where
  each wrapped layer will have its params l2-normalized before computing
  its ``__call__`` output.
  Example usage::
    >>> import jax
    >>> import numpy as np
    >>> from flax import nnx
    >>> class Foo(nnx.Module):
    ...   def __init__(self, rngs: nnx.Rngs):
    ...     self.normed_linear = nnx.WeightNorm(
    ...       nnx.Linear(8, 4, rngs=rngs),
    ...       variable_filters=('kernel',),
    ...       rngs=rngs,
    ...     )
    ...
    ...   def __call__(self, x: jax.Array) -> jax.Array:
    ...     return self.normed_linear(x)
    >>> rng = jax.random.PRNGKey(42)
    >>> model = Foo(rngs=nnx.Rngs(rng))
    >>> x = jax.random.normal(rng, (5, 8))
    >>> y = model(x)
    >>> y.shape
    (5, 4)
    >>> w = model.normed_linear.layer_instance.kernel.value
    >>> col_norms = np.linalg.norm(np.array(w), axis=0)
    >>> np.testing.assert_allclose(col_norms, np.ones(4))

  Args:
    layer_instance: The layer instance to wrap.
    epsilon: The epsilon value for the normalization, by default 1e-12.
    dtype: The dtype of the result, by default infer from input and params.
    param_dtype: The dtype of the parameters, by default float32.
    use_scale: Whether to use a scale parameter.
    scale_init: The initializer for the scale parameter, by default ones.
    feature_axes: The axes to normalize.
    variable_filters: The variable filter, by default ``('kernel',)``.
    rngs: The rng key.
  """
  def __init__(
    self,
    layer_instance: Module,
    *,
    epsilon: float = 1e-12,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_scale: bool = True,
    scale_init: Initializer = initializers.ones,
    feature_axes: Axes | None = -1,
    variable_filters: tuple[str, ...] | None = ('kernel',),
    rngs: rnglib.Rngs,
  ):
    self.layer_instance = layer_instance
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_scale = use_scale
    self.scale_init = scale_init
    self.feature_axes = feature_axes
    self.variable_filters = variable_filters
    self.rngs = rngs

  def __call__(self, x, *args, **kwargs):
    """Compute the l2-norm of the weights in ``self.layer_instance``
    and normalize the weights using this value before computing the
    ``__call__`` output.
    Args:
      x: the input array of the nested layer
      *args: positional arguments to be passed into the call method of the
        underlying layer instance in ``self.layer_instance``.
      **kwargs: keyword arguments to be passed into the call method of the
        underlying layer instance in ``self.layer_instance``.
    Returns:
      Output of the layer using l2-normalized weights.
    """
    if self.variable_filters is None:
      state = nnx.state(self.layer_instance)
    else:
      state = nnx.state(self.layer_instance,
                        [nnx.PathContains(filter) for filter in self.variable_filters])

    def apply_weight_norm(path, vs):
      value = jnp.asarray(vs.value)

      if self.feature_axes is None:
        feature_axes = ()
        reduction_axes = tuple(range(value.ndim))
      else:
        feature_axes = _canonicalize_axes(value.ndim, self.feature_axes)
        reduction_axes = tuple(
          i for i in range(value.ndim) if i not in feature_axes
        )

      feature_shape = [1] * value.ndim
      reduced_feature_shape = []
      for ax in feature_axes:
        feature_shape[ax] = value.shape[ax]
        reduced_feature_shape.append(value.shape[ax])

      value_bar = _l2_normalize(value, axis=reduction_axes, eps=self.epsilon)
      dtargs = [vs.value]

      if self.use_scale:
        scale_path = path + ("scale",)
        try:
          scale = state[scale_path].value
        except KeyError:
          scale = self.scale_init(self.rngs.params(),
                                  reduced_feature_shape,
                                  self.param_dtype)
          state[scale_path] = nnx.Param(scale)

        scale = scale.reshape(feature_shape)

        value_bar *= scale
        dtargs.append(scale)

      dtype = dtypes.canonicalize_dtype(*dtargs, dtype=self.dtype)
      return nnx.Param(jnp.asarray(value_bar, dtype))

    state = nnx.map_state(apply_weight_norm, state)
    nnx.update(self.layer_instance, state)

    return self.layer_instance(x, *args, **kwargs)


class SpectralNorm(Module):
  """Spectral normalization.

  See:

  - https://arxiv.org/abs/1802.05957
  - https://arxiv.org/abs/1805.08318
  - https://arxiv.org/abs/1809.11096

  Spectral normalization normalizes the weight params so that the spectral
  norm of the matrix is equal to 1. This is implemented as a layer wrapper
  where each wrapped layer will have its params spectral normalized before
  computing its ``__call__`` output.

  .. note::
    The initialized variables dict will contain, in addition to a 'params'
    collection, a separate 'batch_stats' collection that will contain a
    ``u`` vector and ``sigma`` value, which are intermediate values used
    when performing spectral normalization. During training, we pass in
    ``update_stats=True`` so that ``u`` and ``sigma`` are updated with
    the most recently computed values using power iteration. This will
    help the power iteration method approximate the true singular value
    more accurately over time. During eval, we pass in ``update_stats=False``
    to ensure we get deterministic behavior from the model.

  Example usage::

    >>> from flax import nnx
    >>> import jax

    >>> rngs = nnx.Rngs(0)
    >>> x = jax.random.normal(jax.random.key(0), (3, 4))
    >>> layer = nnx.SpectralNorm(nnx.Linear(4, 5, rngs=rngs),
    ...                          rngs=rngs)

    >>> nnx.state(layer)
    State({
      'layer_instance': {
        'bias': VariableState( # 5 (20 B)
          type=Param,
          value=Array([0., 0., 0., 0., 0.], dtype=float32)
        ),
        'kernel': VariableState( # 20 (80 B)
          type=Param,
          value=Array([[ 0.5350889 , -0.48486355, -0.4022262 , -0.61925626, -0.46665004],
                 [ 0.31773907,  0.38944173, -0.54608804,  0.84378934, -0.93099   ],
                 [-0.67658   ,  0.0724705 , -0.6101737 ,  0.12972134,  0.877074  ],
                 [ 0.27292168,  0.32105306, -0.2556603 ,  0.4896752 ,  0.19558711]],      dtype=float32)
        )
      },
      'rngs': {
        'default': {
          'count': VariableState( # 1 (4 B)
            type=RngCount,
            value=Array(2, dtype=uint32),
            tag='default'
          ),
          'key': VariableState( # 1 (8 B)
            type=RngKey,
            value=Array((), dtype=key<fry>) overlaying:
            [0 0],
            tag='default'
          )
        }
      }
    })

    >>> y = layer(x, update_stats=True)

  Attributes:
    layer_instance: Module instance that is wrapped with SpectralNorm
    n_steps: How many steps of power iteration to perform to approximate the
      singular value of the weight params.
    epsilon: A small float added to l2-normalization to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    error_on_non_matrix: Spectral normalization is only defined on matrices. By
      default, this module will return scalars unchanged and flatten
      higher-order tensors in their leading dimensions. Setting this flag to
      True will instead throw an error if a weight tensor with dimension greater
      than 2 is used by the layer.
    collection_name: Name of the collection to store intermediate values used
      when performing spectral normalization.
    rngs: The rng key.
  """

  def __init__(
    self,
    layer_instance: Module,
    *,
    n_steps: int = 1,
    epsilon: float = 1e-12,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    error_on_non_matrix: bool = False,
    collection_name: str = 'batch_stats',
    rngs: rnglib.Rngs,
  ):
    self.layer_instance = layer_instance
    self.n_steps = n_steps
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.error_on_non_matrix = error_on_non_matrix
    self.collection_name = collection_name
    self.rngs = rngs

  def __call__(self, x, *args, update_stats: bool, **kwargs):
    """Compute the largest singular value of the weights in ``self.layer_instance``
    using power iteration and normalize the weights using this value before
    computing the ``__call__`` output.

    Args:
      x: the input array of the nested layer
      *args: positional arguments to be passed into the call method of the
        underlying layer instance in ``self.layer_instance``.
      update_stats: if True, update the internal ``u`` vector and ``sigma``
        value after computing their updated values using power iteration. This
        will help the power iteration method approximate the true singular value
        more accurately over time.
      **kwargs: keyword arguments to be passed into the call method of the
        underlying layer instance in ``self.layer_instance``.

    Returns:
      Output of the layer using spectral normalized weights.
    """

    state = nnx.state(self.layer_instance)

    def spectral_normalize(path, vs):
      value = jnp.asarray(vs.value)
      value_shape = value.shape

      # Skip and return value if input is scalar, vector or if number of power
      # iterations is less than 1
      if value.ndim <= 1 or self.n_steps < 1:
        return value
      # Handle higher-order tensors.
      elif value.ndim > 2:
        if self.error_on_non_matrix:
          raise ValueError(
            f'Input is {value.ndim}D but error_on_non_matrix is set to True'
          )
        else:
          value = jnp.reshape(value, (-1, value.shape[-1]))

      u_var_name = (
        self.collection_name
        + '/'
        + '/'.join(str(k) for k in path)
        + '/u'
      )

      try:
        u = state[u_var_name].value
      except KeyError:
        u = jax.random.normal(
          self.rngs.params(),
          (1, value.shape[-1]),
          self.param_dtype,
        )

      sigma_var_name = (
        self.collection_name
        + '/'
        + '/'.join(str(k) for k in path)
        + '/sigma'
      )

      try:
        sigma = state[sigma_var_name].value
      except KeyError:
        sigma = jnp.ones((), self.param_dtype)

      for _ in range(self.n_steps):
        v = _l2_normalize(
          jnp.matmul(u, value.transpose([1, 0])), eps=self.epsilon
        )
        u = _l2_normalize(jnp.matmul(v, value), eps=self.epsilon)

      u = lax.stop_gradient(u)
      v = lax.stop_gradient(v)

      sigma = jnp.matmul(jnp.matmul(v, value), jnp.transpose(u))[0, 0]

      value /= jnp.where(sigma != 0, sigma, 1)
      value_bar = value.reshape(value_shape)

      if update_stats:
        state[u_var_name] = nnx.Param(u)
        state[sigma_var_name] = nnx.Param(sigma)

      dtype = dtypes.canonicalize_dtype(vs.value, u, v, sigma, dtype=self.dtype)
      return nnx.Param(jnp.asarray(value_bar, dtype))

    state = nnx.map_state(spectral_normalize, state)
    nnx.update(self.layer_instance, state)

    return self.layer_instance(x, *args, **kwargs)


class InstanceNorm(Module):
  """Instance normalization (https://arxiv.org/abs/1607.08022v3).

  InstanceNorm normalizes the activations of the layer for each channel (rather
  than across all channels like Layer Normalization), and for each given example
  in a batch independently (rather than across an entire batch like Batch
  Normalization). i.e. applies a transformation that maintains the mean activation
  within each channel within each example close to 0 and the activation standard
  deviation close to 1.

  .. note::
    This normalization operation is identical to LayerNorm and GroupNorm; the
    difference is simply which axes are reduced and the shape of the feature axes
    (i.e. the shape of the learnable scale and bias parameters).

  Example usage::

    >>> from flax import nnx
    >>> import jax
    >>> import numpy as np
    ...
    >>> x = jax.random.normal(jax.random.key(0), (3, 4, 5, 6))
    >>> layer = nnx.InstanceNorm(num_features=6, rngs=nnx.Rngs(0))
    >>> nnx.state(layer)
    State({
      'bias': VariableState( # 6 (24 B)
        type=Param,
        value=Array([0., 0., 0., 0., 0., 0.], dtype=float32)
      ),
      'rngs': {
        'default': {
          'count': VariableState( # 1 (4 B)
            type=RngCount,
            value=Array(2, dtype=uint32),
            tag='default'
          ),
          'key': VariableState( # 1 (8 B)
            type=RngKey,
            value=Array((), dtype=key<fry>) overlaying:
            [0 0],
            tag='default'
          )
        }
      },
      'scale': VariableState( # 6 (24 B)
        type=Param,
        value=Array([1., 1., 1., 1., 1., 1.], dtype=float32)
      )
    })
    >>> y = layer(x)

  Attributes:
    num_features: the number of input features/channels.
    epsilon: A small float added to variance to avoid dividing by zero.
    dtype: the dtype of the result (default: infer from input and params).
    param_dtype: the dtype passed to parameter initializers (default: float32).
    use_bias:  If True, bias (beta) is added.
    use_scale: If True, multiply by scale (gamma). When the next layer is linear
      (also e.g. nn.relu), this can be disabled since the scaling will be done
      by the next layer.
    bias_init: Initializer for bias, by default, zero.
    scale_init: Initializer for scale, by default, one.
    feature_axes: Axes for features. The learned bias and scaling parameters will
      be in the shape defined by the feature axes. All other axes except the batch
      axes (which is assumed to be the leading axis) will be reduced.
    axis_name: the axis name used to combine batch statistics from multiple
      devices. See ``jax.pmap`` for a description of axis names (default: None).
      This is only needed if the model is subdivided across devices, i.e. the
      array being normalized is sharded across devices within a pmap or shard
      map. For SPMD jit, you do not need to manually synchronize. Just make sure
      that the axes are correctly annotated and XLA:SPMD will insert the
      necessary collectives.
    axis_index_groups: groups of axis indices within that named axis
      representing subsets of devices to reduce over (default: None). For
      example, ``[[0, 1], [2, 3]]`` would independently batch-normalize over the
      examples on the first two and last two devices. See ``jax.lax.psum`` for
      more details.
    use_fast_variance: If true, use a faster, but less numerically stable,
      calculation for the variance.
    rngs: The rng key.
  """

  def __init__(
    self,
    num_features: int,
    *,
    epsilon: float = 1e-6,
    dtype: tp.Optional[Dtype] = None,
    param_dtype: Dtype = jnp.float32,
    use_bias: bool = True,
    use_scale: bool = True,
    bias_init: Initializer = initializers.zeros,
    scale_init: Initializer = initializers.ones,
    feature_axes: Axes = -1,
    axis_name: tp.Optional[str] = None,
    axis_index_groups: tp.Any = None,
    use_fast_variance: bool = True,
    rngs: rnglib.Rngs,
  ):
    feature_shape = (num_features,)
    self.scale: nnx.Param[jax.Array] | None
    if use_scale:
      key = rngs.params()
      self.scale = nnx.Param(scale_init(key, feature_shape, param_dtype))
    else:
      self.scale = None

    self.bias: nnx.Param[jax.Array] | None
    if use_bias:
      key = rngs.params()
      self.bias = nnx.Param(bias_init(key, feature_shape, param_dtype))
    else:
      self.bias = None

    self.num_features = num_features
    self.epsilon = epsilon
    self.dtype = dtype
    self.param_dtype = param_dtype
    self.use_bias = use_bias
    self.use_scale = use_scale
    self.bias_init = bias_init
    self.scale_init = scale_init
    self.feature_axes = feature_axes
    self.axis_name = axis_name
    self.axis_index_groups = axis_index_groups
    self.use_fast_variance = use_fast_variance
    self.rngs = rngs

  def __call__(self, x, *, mask: tp.Optional[jax.Array] = None):
    """Applies instance normalization on the input.

    Args:
      x: the inputs
      mask: Binary array of shape broadcastable to ``inputs`` tensor, indicating
        the positions for which the mean and variance should be computed.

    Returns:
      Normalized inputs (the same shape as inputs).
    """
    feature_axes = _canonicalize_axes(x.ndim, self.feature_axes)
    if 0 in feature_axes:
      raise ValueError('The channel axes cannot include the leading dimension '
                       'as this is assumed to be the batch axis.')
    reduction_axes = [i for i in range(1, x.ndim) if i not in feature_axes]

    mean, var = _compute_stats(
      x,
      reduction_axes,
      self.dtype,
      self.axis_name,
      self.axis_index_groups,
      use_fast_variance=self.use_fast_variance,
      mask=mask,
    )

    return _normalize(
      x,
      mean,
      var,
      self.scale.value if self.scale else None,
      self.bias.value if self.bias else None,
      reduction_axes,
      feature_axes,
      self.dtype,
      self.epsilon,
    )