# Copyright 2023-2026 AgentEra(Agently.Tech)
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

"""Standard Action Blocks — strategy-agnostic, TriggerFlow-compatible.

Model-side blocks (ReasonBlock, IntentBlock, ReadBlock, FinalizeBlock) wrap
``async_request_model`` and ``async_read_resource``. Acting blocks (ActBlock,
ObserveBlock) wrap the acting surface and artifact-aware observation.

Reusable by Skills strategies, DAG stages, and future orchestrators.
"""

from agently.builtins.blocks._protocol import FlowBlock
from agently.builtins.blocks.ReasonBlock import ReasonBlock
from agently.builtins.blocks.IntentBlock import IntentBlock
from agently.builtins.blocks.ReadBlock import ReadBlock
from agently.builtins.blocks.FinalizeBlock import FinalizeBlock
from agently.builtins.blocks.ActBlock import ActBlock
from agently.builtins.blocks.ObserveBlock import ObserveBlock

__all__ = [
    "FlowBlock",
    "ReasonBlock",
    "IntentBlock",
    "ReadBlock",
    "FinalizeBlock",
    "ActBlock",
    "ObserveBlock",
]
