"""The user interface to define skip connections."""
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    Generator,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from torch import Tensor, nn

from torchgpipe.microbatch import Batch
from torchgpipe.skip.namespace import Namespace
from torchgpipe.skip.tracker import current_skip_tracker

__all__ = ["skippable", "stash", "pop", "verify_skippables"]


Tensors = Tuple[Tensor, ...]
TensorOrTensors = Union[Tensor, Tensors]

StashPop = Union["stash", "pop"]
StashPopGenerator = Generator[StashPop, Optional[Tensor], TensorOrTensors]
if TYPE_CHECKING:
    SkippableModule = nn.Module[Union[StashPopGenerator, TensorOrTensors]]
else:
    SkippableModule = nn.Module

T = TypeVar("T", bound="Skippable")


class Skippable(nn.Module):
    """The base class for skippable modules.

    Do not use this class directly. Define a subclass by :func:`skippable`
    instead.

    """

    module_cls: ClassVar[Type[SkippableModule]]
    stashable_names: ClassVar[FrozenSet[str]]
    poppable_names: ClassVar[FrozenSet[str]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.module = self.module_cls(*args, **kwargs)  # type: ignore
        self.namespaces: Dict[str, Namespace] = {}

    def __repr__(self) -> str:
        return f"@skippable({self.module})"

    def namespaced(self, name: str) -> Tuple[Namespace, str]:
        """Prepends namespace for the given skip name."""
        ns = self.namespaces.get(name)
        ns = cast(Namespace, ns)
        return (ns, name)

    def stashable(self) -> Iterable[Tuple[Namespace, str]]:
        """Iterates over namespaced skip names to be stashed."""
        for name in self.stashable_names:
            yield self.namespaced(name)

    def poppable(self) -> Iterable[Tuple[Namespace, str]]:
        """Iterates over namespaced skip names to be popped."""
        for name in self.poppable_names:
            yield self.namespaced(name)

    def isolate(self: T, ns: Namespace, *, only: Optional[Iterable[str]] = None) -> T:
        r"""Isolates a specified subset or the whole set of skip tensors into a
        namespace. In a single sequential module, skip tensors with the same
        name are not allowed unless they are isolated by different namespaces.

        Here's an example using the same name for skip tensors twice. Each pair
        of ``Layer1`` and ``Layer2`` is isolated with its own namespace ``ns1``
        and ``ns2``. There is no conflict anymore::

            ns1 = Namespace()
            ns2 = Namespace()

            model = nn.Sequential(
                Layer1().isolate(ns1),
                Layer1().isolate(ns2),
                Layer2(),
                Layer3().isolate(ns2),
                Layer3().isolate(ns1),
            )

        When `only` parameter is omitted, all skip tensors are isolated. You
        can isolate a subset of skip tensors by passing `only` parameter::

            ns_alice = Namespace()
            ns_bob = Namespace()

            model = nn.Sequential(
                ...
                StashStashPop().isolate(ns_alice, only=['alice']) \
                               .isolate(ns_bob, only=['bob']),
                ...
            )

        Args:
            ns (Namespace):
                namespace for isolation

        Keyword Args:
            only (iterable of strs):
                names of specific skip tensors to be isolated (omit this option
                to isolate all skip tensors declared in this module)

        Returns:
            this module itself

        """
        names: Iterable[str]

        if only is None:
            names = self.stashable_names | self.poppable_names
        else:
            names = set(only)

        for name in names:
            self.namespaces[name] = ns

        return self

    def dispatch(
        self,
        input: TensorOrTensors,
        handle_stash: Callable[[str, Optional[Tensor]], None],
        handle_pop: Callable[[str], Optional[Tensor]],
    ) -> TensorOrTensors:
        """Dispatches :class:`stash` or :class:`pop` commands generated by the
        module's ``forward()``.
        """
        generator = self.module(input)

        if not isinstance(generator, Generator):
            # The underlying module returned output without any yield.
            output = generator
            return output

        try:
            op = next(generator)

            while True:
                if isinstance(op, stash):
                    handle_stash(op.name, op.tensor)
                    op = next(generator)
                    continue

                if isinstance(op, pop):
                    tensor = handle_pop(op.name)
                    op = generator.send(tensor)
                    continue

                raise TypeError("%r is not a command from @skippable" % op)

        except StopIteration as stop:
            output = stop.args[0]
            return output

    def forward(self, input: TensorOrTensors) -> TensorOrTensors:  # type: ignore
        """Performs the forward propagation. :class:`stash` or :class:`pop`
        commands will be handled by portals silently. The portals won't be
        exposed to users.

        Raises:
            RuntimeError:
                illegal 'stash' or 'pop' is found.

        """
        skip_tracker = current_skip_tracker()
        stashed_tensors: Dict[str, Optional[Tensor]] = {}

        # Load skip tensors that might be popped.
        poppable_tensors = {}
        batch = Batch(input)
        for ns, name in self.poppable():
            try:
                poppable_tensors[name] = skip_tracker.load(batch, ns, name)
            except KeyError:
                raise RuntimeError(f"'{name}' has not been stashed")
        input = batch.tensor_or_tensors

        # Handle skip commands.
        def handle_stash(name: str, tensor: Optional[Tensor]) -> None:
            if name not in self.stashable_names:
                raise RuntimeError(f"'{name}' has not been declared as stashable")
            stashed_tensors[name] = tensor

        def handle_pop(name: str) -> Optional[Tensor]:
            if name not in self.poppable_names:
                raise RuntimeError(f"'{name}' has not been declared as poppable")
            return poppable_tensors.pop(name)

        output = self.dispatch(input, handle_stash, handle_pop)

        # All declared skips must be stashed or popped.
        not_stashed = self.stashable_names - stashed_tensors.keys()
        if not_stashed:
            comma_names = ", ".join("'%s'" % n for n in not_stashed)
            raise RuntimeError(f"{comma_names} must be stashed but have not")

        not_popped = poppable_tensors.keys()
        if not_popped:
            comma_names = ", ".join("'%s'" % n for n in not_popped)
            raise RuntimeError(f"{comma_names} must be popped but have not")

        # Save stashed skip tensors.
        batch = Batch(output)
        for ns, name in self.stashable():
            tensor = stashed_tensors[name]
            skip_tracker.save(batch, ns, name, tensor)
        output = batch.tensor_or_tensors

        return output


# TODO(sublee): Move to above of Skippable class for better read flow.
def skippable(
    stash: Iterable[str] = (), pop: Iterable[str] = (),
) -> Callable[[Type[SkippableModule]], Type[Skippable]]:
    """The decorator to define a :class:`nn.Module <torch.nn.Module>` with skip
    connections. Decorated modules are called "skippable". This functionality
    works perfectly fine even when the module is not wrapped by
    :class:`~torchgpipe.GPipe`.

    Each skip tensor is managed by its name. Before manipulating skip tensors,
    a skippable module must statically declare the names for skip tensors by
    `stash` and/or `pop` parameters. Skip tensors with pre-declared name can be
    stashed by ``yield stash(name, tensor)`` or popped by ``tensor = yield
    pop(name)``.

    Here is an example with three layers. A skip tensor named "1to3" is stashed
    and popped at the first and last layer, respectively::

        @skippable(stash=['1to3'])
        class Layer1(nn.Module):
            def forward(self, input):
                yield stash('1to3', input)
                return f1(input)

        class Layer2(nn.Module):
            def forward(self, input):
                return f2(input)

        @skippable(pop=['1to3'])
        class Layer3(nn.Module):
            def forward(self, input):
                skip_1to3 = yield pop('1to3')
                return f3(input) + skip_1to3

        model = nn.Sequential(Layer1(), Layer2(), Layer3())

    One skippable module can stash or pop multiple skip tensors::

        @skippable(stash=['alice', 'bob'], pop=['carol'])
        class StashStashPop(nn.Module):
            def forward(self, input):
                yield stash('alice', f_alice(input))
                yield stash('bob', f_bob(input))
                carol = yield pop('carol')
                return input + carol

    Every skip tensor must be associated with exactly one pair of `stash` and
    `pop`. :class:`~torchgpipe.GPipe` checks this restriction automatically
    when wrapping a module. You can also check the restriction by
    :func:`~torchgpipe.skip.verify_skippables` without
    :class:`~torchgpipe.GPipe`.

    .. note::

        :func:`@skippable <skippable>` changes the type of the wrapped class.
        But currently (mypy v0.740), mypy could not understand class decorators
        yet (`#3135 <https://github.com/python/mypy/issues/3135>`_).

        There are two workarounds:

        1. Naively ignore type errors by ``# type: ignore``.
        2. Use ``skippable()()`` as a function instead of a decorator.

    .. seealso:: :ref:`Long Skip Connections`

    """
    stashable_names = frozenset(stash)
    poppable_names = frozenset(pop)

    def extend_skippable(module_cls: Type[SkippableModule]) -> Type[Skippable]:
        name = module_cls.__name__
        bases = (Skippable,)
        attrs = {
            "module_cls": module_cls,
            "stashable_names": stashable_names,
            "poppable_names": poppable_names,
        }
        return type(name, bases, attrs)

    return extend_skippable


class stash:
    """The command to stash a skip tensor.

    ::

        def forward(self, input):
            yield stash('name', input)
            return f(input)

    Args:
        name (str): name of skip tensor
        input (torch.Tensor or None): tensor to pass to the skip connection

    """

    __slots__ = ("name", "tensor")

    def __init__(self, name: str, tensor: Optional[Tensor]) -> None:
        self.name = name
        self.tensor = tensor


class pop:
    """The command to pop a skip tensor.

    ::

        def forward(self, input):
            skip = yield pop('name')
            return f(input) + skip

    Args:
        name (str): name of skip tensor

    Returns:
        the skip tensor previously stashed by another layer under the same name

    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def verify_skippables(module: nn.Sequential) -> None:
    """Verifies if the underlying skippable modules satisfy integrity.

    Every skip tensor must have only one pair of `stash` and `pop`. If there
    are one or more unmatched pairs, it will raise :exc:`TypeError` with the
    detailed messages.

    Here are a few failure cases. :func:`verify_skippables` will report failure
    for these cases::

        # Layer1 stashes "1to3".
        # Layer3 pops "1to3".

        nn.Sequential(Layer1(), Layer2())
        #               └──── ?

        nn.Sequential(Layer2(), Layer3())
        #                   ? ────┘

        nn.Sequential(Layer1(), Layer2(), Layer3(), Layer3())
        #               └───────────────────┘       ^^^^^^

        nn.Sequential(Layer1(), Layer1(), Layer2(), Layer3())
        #             ^^^^^^      └───────────────────┘

    To use the same name for multiple skip tensors, they must be isolated by
    different namespaces. See :meth:`isolate()
    <torchgpipe.skip.skippable.Skippable.isolate>`.

    Raises:
        TypeError:
            one or more pairs of `stash` and `pop` are not matched.

    """
    stashed: Set[Tuple[Namespace, str]] = set()
    popped: Set[Tuple[Namespace, str]] = set()
    msgs: List[str] = []

    for layer_name, layer in module.named_children():
        if not isinstance(layer, Skippable):
            continue

        for name in layer.stashable_names & layer.poppable_names:
            msg = f"'{layer_name}' declared '{name}' both as stashable and as poppable"
            msgs.append(msg)

        for ns, name in layer.stashable():
            if name in layer.poppable_names:
                continue

            if (ns, name) in stashed:
                msg = (
                    f"'{layer_name}' redeclared '{name}' as stashable "
                    "but not isolated by namespace"
                )
                msgs.append(msg)
                continue

            stashed.add((ns, name))

        for ns, name in layer.poppable():
            if name in layer.stashable_names:
                continue

            if (ns, name) in popped:
                msg = (
                    f"'{layer_name}' redeclared '{name}' as poppable "
                    "but not isolated by namespace"
                )
                msgs.append(msg)
                continue

            if (ns, name) not in stashed:
                msg = f"'{layer_name}' declared '{name}' as poppable but it was not stashed"
                msgs.append(msg)
                continue

            popped.add((ns, name))

    for (_, name) in stashed - popped:
        msg = f"no module declared '{name}' as poppable but stashed"
        msgs.append(msg)

    if msgs:
        raise TypeError(
            "one or more pairs of stash and pop do not match:\n\n%s"
            "" % "\n".join("* %s" % x for x in msgs)
        )
