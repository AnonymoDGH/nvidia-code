
from typing import Dict, Any, List, Tuple

class ExampleTool:
    @property
    def name(self) -> str:
        return "example_tool"
    
    @property
    def description(self) -> str:
        return "Herramienta de ejemplo"
    
    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "input": {
                "type": "string",
                "description": "Entrada de ejemplo"
            }
        }
    
    @property
    def required_params(self) -> List[str]:
        return ["input"]
    
    def execute(self, **kwargs) -> str:
        return f"Resultado: {kwargs.get('input', '')}"
    
    def validate(self, **kwargs) -> Tuple[bool, str]:
        if 'input' not in kwargs:
            return False, "Falta input"
        return True, ""
