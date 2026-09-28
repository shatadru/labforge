"""Centralized RPC method registry for LabForge agent.

Provides a clean, extensible way to register and dispatch RPC methods.
Each method is registered once and can be looked up by name.
"""

from typing import Callable, Dict, TypeVar

RPC_METHODS = TypeVar("RPC_Methods", bound=Callable)


class RPCRegistry:
    """Registry for LabForge RPC methods.

    Methods are registered by name and can be looked up dynamically.
    Provides a simple registration API and a lookup method.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, RPC_Methods] = {}

    def register(self, method_name: str, func: RPC_Methods) -> None:
        """Register a method by name.

        Args:
            method_name: The RPC method name (e.g., "get_vm", "list_vms").
            func: The callable that implements the RPC method.
        """
        self._registry[method_name] = func

    def get(self, method_name: str) -> RPC_Methods:
        """Retrieve a registered method by name.

        Args:
            method_name: The RPC method name.

        Returns:
            The registered method callable.

        Raises:
            KeyError: If the method is not registered.
        """
        if method_name not in self._registry:
            raise KeyError(f"RPC method '{method_name}' not found in registry")
        return self._registry[method_name]

    def all_methods(self) -> list:
        """Return a list of all registered method names.

        Returns:
            List of method names.
        """
        return list(self._registry.keys())


# Global singleton instance
_rpc_registry = RPCRegistry()
