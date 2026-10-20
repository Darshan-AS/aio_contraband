"""Monadic Result type (`Ok[T] | Err[E]`) with PEP 696 default error type and pure combinators."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from contraband.core.errors import ContrabandError


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """Represents a successful computation holding an immutable value."""

    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Err[E = ContrabandError]:
    """Represents a failed computation holding an immutable domain error."""

    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True


type Result[T, E = ContrabandError] = Ok[T] | Err[E]


def map_result[T, E, U](result: Result[T, E], fn: Callable[[T], U]) -> Result[U, E]:
    """Applies `fn` to the value inside `Ok`, leaving `Err` untouched."""
    match result:
        case Ok(val):
            return Ok(fn(val))
        case Err() as err:
            return err


def map_err[T, E, F](result: Result[T, E], fn: Callable[[E], F]) -> Result[T, F]:
    """Applies `fn` to the error inside `Err`, leaving `Ok` untouched."""
    match result:
        case Ok() as ok:
            return ok
        case Err(err):
            return Err(fn(err))


def and_then[T, E, U](result: Result[T, E], fn: Callable[[T], Result[U, E]]) -> Result[U, E]:
    """Monadic bind (`flatMap`) for chaining fallible pure computations."""
    match result:
        case Ok(val):
            return fn(val)
        case Err() as err:
            return err


def unwrap_or[T, E](result: Result[T, E], default: T) -> T:
    """Returns the `Ok` value or `default` if `Err`."""
    match result:
        case Ok(val):
            return val
        case Err():
            return default


def collect_results[T, E](items: Iterable[Result[T, E]]) -> Result[tuple[T, ...], E]:
    """Transforms an iterable of `Result[T, E]` into `Result[tuple[T, ...], E]`."""
    acc: list[T] = []
    for item in items:
        match item:
            case Ok(val):
                acc.append(val)
            case Err() as err:
                return err
    return Ok(tuple(acc))
