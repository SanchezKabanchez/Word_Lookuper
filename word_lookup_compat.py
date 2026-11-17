"""Legacy word_lookup module facade; production services never import it."""
import types


def expose(namespace: dict, bindings: dict, service, names: tuple) -> None:
    for name in names:
        namespace[name] = getattr(service, name)
        bindings.setdefault(name, []).append((service, name))


class CompatibilityModule(types.ModuleType):
    def __getattribute__(self, name):
        namespace = types.ModuleType.__getattribute__(self, "__dict__")
        targets = namespace.get("_COMPAT_BINDINGS", {}).get(name)
        if targets:
            service, attribute = targets[0]
            return getattr(service, attribute)
        return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        namespace = types.ModuleType.__getattribute__(self, "__dict__")
        for service, attribute in namespace.get("_COMPAT_BINDINGS", {}).get(name, ()):
            setattr(service, attribute, value)
        types.ModuleType.__setattr__(self, name, value)


def install_compatibility(module, bindings: dict) -> None:
    module._COMPAT_BINDINGS = bindings
    module.__class__ = CompatibilityModule
