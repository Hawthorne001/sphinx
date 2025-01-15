"""What follows is awful and will be gone in Sphinx 9.

Instances of _StrPath should not be constructed except in Sphinx itself.
Consumers of Sphinx APIs should prefer using ``pathlib.Path`` objects
where possible. _StrPath objects can be treated as equivalent to ``Path``,
save that ``_StrPath.replace`` is overridden with ``str.replace``.

To continue treating path-like objects as strings, use ``os.fspath``,
or explicit string coercion.

In Sphinx 9, ``Path`` objects will be expected and returned in all instances
that ``_StrPath`` is currently used.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path, PosixPath, PurePath, WindowsPath
from typing import TYPE_CHECKING, overload

from sphinx.deprecation import RemovedInSphinx90Warning

if TYPE_CHECKING:
    from typing import Any

_STR_METHODS = frozenset(str.__dict__)
_PATH_NAME = Path().__class__.__name__

_MSG = (
    'Sphinx 9 will drop support for representing paths as strings. '
    'Use "pathlib.Path" or "os.fspath" instead.'
)

# https://docs.python.org/3/library/stdtypes.html#typesseq-common
# https://docs.python.org/3/library/stdtypes.html#string-methods

if sys.platform == 'win32':

    class _StrPath(WindowsPath):
        def replace(  # type: ignore[override]
            self, old: str, new: str, count: int = -1, /
        ) -> str:
            # replace exists in both Path and str;
            # in Path it makes filesystem changes, so we use the safer str version
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__().replace(old, new, count)  # NoQA:  PLC2801

        def __getattr__(self, item: str) -> Any:
            if item in _STR_METHODS:
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return getattr(self.__str__(), item)
            msg = f'{_PATH_NAME!r} has no attribute {item!r}'
            raise AttributeError(msg)

        def __add__(self, other: str) -> str:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__() + other

        def __bool__(self) -> bool:
            if not self.__str__():
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return False
            return True

        def __contains__(self, item: str) -> bool:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return item in self.__str__()

        def __eq__(self, other: object) -> bool:
            if isinstance(other, PurePath):
                return super().__eq__(other)
            if isinstance(other, str):
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return self.__str__() == other
            return NotImplemented

        def __hash__(self) -> int:
            return super().__hash__()

        def __getitem__(self, item: int | slice) -> str:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__()[item]

        def __len__(self) -> int:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return len(self.__str__())

else:

    class _StrPath(PosixPath):
        def replace(  # type: ignore[override]
            self, old: str, new: str, count: int = -1, /
        ) -> str:
            # replace exists in both Path and str;
            # in Path it makes filesystem changes, so we use the safer str version
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__().replace(old, new, count)  # NoQA:  PLC2801

        def __getattr__(self, item: str) -> Any:
            if item in _STR_METHODS:
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return getattr(self.__str__(), item)
            msg = f'{_PATH_NAME!r} has no attribute {item!r}'
            raise AttributeError(msg)

        def __add__(self, other: str) -> str:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__() + other

        def __bool__(self) -> bool:
            if not self.__str__():
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return False
            return True

        def __contains__(self, item: str) -> bool:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return item in self.__str__()

        def __eq__(self, other: object) -> bool:
            if isinstance(other, PurePath):
                return super().__eq__(other)
            if isinstance(other, str):
                warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
                return self.__str__() == other
            return NotImplemented

        def __hash__(self) -> int:
            return super().__hash__()

        def __getitem__(self, item: int | slice) -> str:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return self.__str__()[item]

        def __len__(self) -> int:
            warnings.warn(_MSG, RemovedInSphinx90Warning, stacklevel=2)
            return len(self.__str__())


class _StrPathProperty:
    def __init__(self) -> None:
        self.instance_attr: str = ''

    def __set_name__(self, owner: object, name: str) -> None:
        self.instance_attr = f'_{name}'  # i.e. '_srcdir'

    @overload
    def __get__(self, obj: None, objtype: None) -> _StrPathProperty: ...

    @overload
    def __get__(self, obj: object, objtype: type[object]) -> _StrPath: ...

    def __get__(
        self, obj: object | None, objtype: type[object] | None = None
    ) -> _StrPathProperty | _StrPath:
        if obj is None:
            return self
        if not self.instance_attr:
            raise AttributeError
        return getattr(obj, self.instance_attr)

    def __set__(self, obj: Any, value: _StrPath | Path) -> None:
        try:
            setattr(obj, self.instance_attr, _StrPath(value))
        except TypeError as err:
            cls_name = type(obj).__qualname__
            name = self.instance_attr.removeprefix('_')
            msg = f'{cls_name}.{name} may only be set to path-like objects'
            raise TypeError(msg) from err

    def __delete__(self, obj: Any) -> None:
        delattr(obj, self.instance_attr)
