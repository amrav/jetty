"""Module discovery and enable/disable (SPEC.md §4.4, §5).

Two rules make "modular" mean something here:

1. **Nothing is enabled by default.** A module runs only if config says
   `[modules.<name>] enabled = true`. A sidecar that answers questions nobody
   configured it to answer is a liability, and "it was on by default" is how
   that happens.

2. **A disabled module is not imported.** Registration is by factory, so
   disabling the LLM proxy means its dependencies are never even loaded. This
   keeps an unused module from being able to break boot, and keeps its
   third-party imports out of the process image entirely.

Unknown module names in config are a hard error rather than a warning: a typo in
`[modules.ath]` would otherwise silently leave auth disabled, and this is a
security component, so a config that does not mean what it says must not boot.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from jetty.modules.base import Module

#: name -> factory. Factories, not instances: an unregistered-because-disabled
#: module must never be constructed or have its imports executed.
ModuleFactory = Callable[[Mapping[str, Any]], Module]

_REGISTRY: dict[str, ModuleFactory] = {}


def register(name: str, factory: ModuleFactory) -> None:
    if name in _REGISTRY:
        raise ValueError(f"module {name!r} is already registered")
    _REGISTRY[name] = factory


def known_modules() -> list[str]:
    return sorted(_REGISTRY)


class UnknownModuleError(Exception):
    """Config names a module that does not exist. Fail at boot, loudly."""


def build_enabled(module_settings: Mapping[str, Mapping[str, Any]]) -> list[Module]:
    """Instantiate exactly the modules config turned on.

    `module_settings` is the `[modules]` table: {name: {enabled: bool, ...}}.
    """
    unknown = sorted(set(module_settings) - set(_REGISTRY))
    if unknown:
        raise UnknownModuleError(
            f"unknown module(s) in config: {', '.join(unknown)}; "
            f"known modules: {', '.join(known_modules())}"
        )

    built: list[Module] = []
    for name in sorted(module_settings):
        settings = module_settings[name]
        if not settings.get("enabled", False):
            continue
        module = _REGISTRY[name](settings)
        if module.name != name:
            raise ValueError(
                f"module registered as {name!r} reports name {module.name!r}; "
                "the config key and the mount segment must agree"
            )
        built.append(module)
    return built


def _register_builtins() -> None:
    """Register the modules shipped in this repository.

    Imports are deferred into the factory so that a disabled module's
    dependencies are never imported (rule 2 above).
    """

    def _reference(settings: Mapping[str, Any]) -> Module:
        from jetty.modules.reference.module import ReferenceModule

        return ReferenceModule(settings)

    register("reference", _reference)

    # `auth` and `llmproxy` are specified in spec/ and not yet implemented.
    # They are deliberately NOT registered: naming them here before they exist
    # would let `enabled = true` boot a sidecar that answers auth questions
    # with a stub. Until the module lands, `[modules.auth]` fails at boot with
    # UnknownModuleError, which is the correct fail-closed behaviour.


_register_builtins()
