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

# Copyright 2024 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Base layers."""

from typing import Any, Sequence, TypeAlias

from flax import nnx
import flax.linen as nn
import jax.numpy as jnp
from jaxtyping import Array, ArrayLike  # pylint: disable=g-importing-member,g-multiple-import


Shape: TypeAlias = Sequence[int | Any]


class Einsum(nnx.Module):
  """Einsum is a convenience module for parameterized tensor multiplication."""

  def __init__(self, shape: Shape, *, rngs: nnx.Rngs):
    self.w = nnx.Param(nn.initializers.normal()(rngs.params(), shape))

  def __call__(self, eqn: str, x: ArrayLike) -> Array:
    return jnp.einsum(eqn, x, self.w.value)

  @property
  def shape(self) -> Shape:
    return self.w.value.shape


class RMSNorm(nnx.Module):
  """RMSNorm layer."""

  def __init__(self, dim: int, *, rngs: nnx.Rngs):
    self.scale = nnx.Param(nn.initializers.zeros_init()(rngs.params(), dim))

  def __call__(self, x):
    var = jnp.mean(jnp.square(x), axis=-1, keepdims=True)
    normed_inputs = jnp.asarray(x * jnp.reciprocal(jnp.sqrt(var + 1e-06)))
    # normed_inputs is a rank-K tensor, K > 1 (K is typically 2 or 3). scale is
    # a rank-1 tensor. To avoid implicit rank-promotion, reshape scale to
    # a (1, ..., 1, D) tensor, so the rank of scale matches normed_inputs.
    scale = jnp.expand_dims(self.scale.value, axis=range(len(x.shape) - 1))
    normed_inputs = normed_inputs * (1 + scale)
    return normed_inputs
