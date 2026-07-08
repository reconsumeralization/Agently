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


import inspect

from typing import Any, Callable

from agently.core import BaseAgent


class AutoFuncExtension(BaseAgent):
    @staticmethod
    def _resolve_output_contract(return_annotation: Any) -> tuple[Any, str | None]:
        if return_annotation is inspect.Signature.empty:
            return str, None
        if isinstance(return_annotation, dict):
            return return_annotation, None
        if return_annotation in (dict, list, tuple):
            return return_annotation, None
        return {"return_value": (return_annotation,)}, "return_value"

    def auto_func(self, func: Callable):
        if inspect.iscoroutinefunction(func):

            async def async_wrapper(*args, **kwargs):
                signature = inspect.signature(func)
                arguments = signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                input_dict = {}
                for param in signature.parameters:
                    input_dict.update({param: arguments.arguments[param]})
                # generate instruction
                instruction = inspect.getdoc(func)
                # generate output dict
                output_dict, unwrap_key = self._resolve_output_contract(signature.return_annotation)
                result = await self.input(input_dict).instruct(instruction).output(output_dict).async_start()
                if unwrap_key is not None and isinstance(result, dict):
                    return result.get(unwrap_key)
                return result

            return async_wrapper
        elif (
            inspect.isfunction(func) and not inspect.isasyncgenfunction(func) and not inspect.isgeneratorfunction(func)
        ):

            def wrapper(*args, **kwargs):
                signature = inspect.signature(func)
                arguments = signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                input_dict = {}
                for param in signature.parameters:
                    input_dict.update({param: arguments.arguments[param]})
                # generate instruction
                instruction = inspect.getdoc(func)
                # generate output dict
                output_dict, unwrap_key = self._resolve_output_contract(signature.return_annotation)
                result = self.input(input_dict).instruct(instruction).output(output_dict).start()
                if unwrap_key is not None and isinstance(result, dict):
                    return result.get(unwrap_key)
                return result

            return wrapper
        else:
            raise TypeError(f"Error: Cannot decorate generator as an automatic function.\nFunction: { func }")
