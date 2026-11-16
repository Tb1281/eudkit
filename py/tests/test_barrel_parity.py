"""Cycle 2 root barrel and cross-frontend export parity.

The Python root (``py/__init__.py``) is a curated barrel while the TypeScript
root (``ts/index.ts``) re-exports whole modules. This test pins the Cycle 2
absorption surface:

* the root ``__all__`` is duplicate-free and every listed name resolves;
* every absorbed Python module's ``__all__`` is promoted to the root;
* the runtime exports of the absorbed TypeScript modules are reachable from
  both ``ts/index.ts`` and the Python root; and
* snake_case/camelCase aliases are the same objects on modules and classes.

Mirror of the TypeScript package-root checks; Node is invoked with
``--experimental-strip-types`` so the source tree is inspected directly.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import unittest
from pathlib import Path
from types import ModuleType

import py

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules absorbed into the flat root during Cycle 2.
ABSORBED_MODULES = ("mathf", "random", "player", "location", "scstate")

#: Absorbed ``view`` exports: CUnitView, flags, bit helpers, and split helpers.
VIEW_RUNTIME_EXPORTS = (
    "CUnitView",
    "GROUP_FLAGS_TARGET",
    "CURRENT_PLAYER",
    "AvailabilityFlags",
    "GroupFlags",
    "PathingFlags",
    "StatusFlags",
    "readBit",
    "readBitVar",
    "read_bit",
    "read_bit_var",
    "writeBit",
    "writeMask",
    "write_bit",
    "write_mask",
    "splitDword",
    "splitWord",
    "split_dword",
    "split_word",
)

#: Absorbed ``queue`` exports.
QUEUE_RUNTIME_EXPORTS = ("ObjectPool", "POOL_NULL")

#: Camel names whose Python spelling is not mechanical.
ALIAS_EXCEPTIONS = {
    "mathf": {
        "atan2Fixed8": "atan2_256",
        "lengthdirFixed8": "lengthdir_256",
    },
    "random": {"dwRand": "dwrand"},
}

#: Camel module names that are intentionally not paired here (see class test).
MODULE_ALIAS_SKIP = {"location": {"setRect"}}

#: Explicit camel→snake method aliases required by Cycle 2.
CLASS_ALIAS_PAIRS = {
    ("location", "Location"): {"setRect": "set_rect"},
    ("view", "CUnitView"): {
        "nextUnit": "next_unit",
        "prevUnit": "prev_unit",
        "spriteEpd": "sprite_epd",
        "isBurrowed": "is_burrowed",
        "setInvincible": "set_invincible",
        "giveTo": "give_to",
        "setColor": "set_color",
        "setLocation": "set_location",
    },
    ("queue", "ObjectPool"): {
        "freeList": "free_list",
        "allocateObject": "allocate",
        "releaseObject": "release",
    },
}

_NODE_SCRIPT = """
(async () => {
  const modules = ["mathf", "random", "player", "location", "scstate", "view", "queue"];
  const exports = {};
  for (const name of modules) {
    exports[name] = Object.keys(await import(`./ts/${name}.ts`)).sort();
  }
  exports.index = Object.keys(await import("./ts/index.ts")).sort();
  console.log(JSON.stringify(exports));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""

_CAMEL_BOUNDARY = re.compile(r"(.)([A-Z][a-z]+)")
_LOWER_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def _camel_to_snake(name: str) -> str:
    first = _CAMEL_BOUNDARY.sub(r"\1_\2", name)
    return _LOWER_BOUNDARY.sub(r"\1_\2", first).lower()


def _is_camel(name: str) -> bool:
    return name[:1].islower() and any(char.isupper() for char in name)


def _load_ts_exports() -> dict[str, list[str]]:
    completed = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            "-e",
            _NODE_SCRIPT,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


class TestRootBarrel(unittest.TestCase):
    def test_all_has_no_duplicates(self) -> None:
        self.assertEqual(
            [name for name in set(py.__all__) if py.__all__.count(name) > 1],
            [],
        )

    def test_every_listed_name_resolves_from_the_root(self) -> None:
        for name in py.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(py, name), f"py.{name} is missing")
                self.assertIs(getattr(py, name), getattr(py, name))

    def test_cycle_2_names_are_listed_and_resolve(self) -> None:
        for name in (
            "GROUP_FLAGS_TARGET",
            "CURRENT_PLAYER",
            "MathOperand",
            "RandSeed",
            "LocationOperand",
            "CoordOperand",
            "PlayerOperand",
        ):
            with self.subTest(name=name):
                self.assertIn(name, py.__all__)
                self.assertTrue(hasattr(py, name))


class TestAbsorbedModuleAll(unittest.TestCase):
    def test_every_absorbed_module_export_is_promoted_to_root(self) -> None:
        for module_name in ABSORBED_MODULES:
            module = importlib.import_module(f"py.{module_name}")
            exported = set(module.__all__)
            if module_name == "location":
                # ``setRect`` is a ``Location.set_rect`` method alias; the class
                # test below pins it without promoting the module-level binding.
                exported.discard("setRect")
            for name in sorted(exported):
                with self.subTest(module=module_name, name=name):
                    self.assertIn(name, py.__all__)
                    self.assertTrue(hasattr(py, name))


class TestTypeScriptRootParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ts = _load_ts_exports()

    def test_ts_index_imports_with_the_expected_modules(self) -> None:
        self.assertIn("index", self.ts)
        for module_name in (*ABSORBED_MODULES, "view", "queue"):
            with self.subTest(module=module_name):
                self.assertTrue(self.ts[module_name])

    def test_absorbed_runtime_exports_exist_in_ts_and_py(self) -> None:
        runtime: set[str] = set()
        for module_name in ABSORBED_MODULES:
            runtime.update(self.ts[module_name])
        runtime.update(VIEW_RUNTIME_EXPORTS)
        runtime.update(QUEUE_RUNTIME_EXPORTS)

        index_exports = set(self.ts["index"])
        self.assertGreater(len(runtime), 0)
        for name in sorted(runtime):
            with self.subTest(name=name):
                self.assertIn(name, index_exports, f"ts/index.ts lost {name}")
                self.assertIn(name, py.__all__, f"py.__all__ lost {name}")
                self.assertTrue(hasattr(py, name), f"py.{name} is missing")

    def test_view_subset_is_present_in_view_module(self) -> None:
        view_exports = set(self.ts["view"])
        for name in VIEW_RUNTIME_EXPORTS:
            with self.subTest(name=name):
                self.assertIn(name, view_exports)

    def test_queue_subset_is_present_in_queue_module(self) -> None:
        queue_exports = set(self.ts["queue"])
        for name in QUEUE_RUNTIME_EXPORTS:
            with self.subTest(name=name):
                self.assertIn(name, queue_exports)


class TestAliasIdentity(unittest.TestCase):
    def test_module_camel_aliases_match_their_snake_names(self) -> None:
        for module_name in ABSORBED_MODULES:
            module: ModuleType = importlib.import_module(f"py.{module_name}")
            exceptions = ALIAS_EXCEPTIONS.get(module_name, {})
            skipped = MODULE_ALIAS_SKIP.get(module_name, set())
            checked = 0
            for camel in module.__all__:
                if camel in skipped or not _is_camel(camel):
                    continue
                snake = exceptions.get(camel, _camel_to_snake(camel))
                with self.subTest(module=module_name, camel=camel, snake=snake):
                    self.assertIn(snake, module.__all__)
                    self.assertIs(getattr(module, camel), getattr(module, snake))
                    self.assertIs(getattr(py, camel), getattr(py, snake))
                checked += 1
            self.assertGreater(checked, 0, f"no camel aliases checked in {module_name}")

    def test_class_method_camel_aliases_match_their_snake_names(self) -> None:
        for (module_name, class_name), pairs in CLASS_ALIAS_PAIRS.items():
            module = importlib.import_module(f"py.{module_name}")
            cls = getattr(module, class_name)
            for camel, snake in pairs.items():
                with self.subTest(module=module_name, cls=class_name, camel=camel):
                    self.assertIn(camel, vars(cls))
                    self.assertIn(snake, vars(cls))
                    self.assertIs(getattr(cls, camel), getattr(cls, snake))


if __name__ == "__main__":
    unittest.main()
