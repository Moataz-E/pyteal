from typing import (
    Union,
    Sequence,
    TypeVar,
    Generic,
    Final,
    cast,
)
from abc import abstractmethod

from pyteal.types import TealType, require_type
from pyteal.errors import TealInputError
from pyteal.ast.expr import Expr
from pyteal.ast.seq import Seq
from pyteal.ast.int import Int
from pyteal.ast.if_ import If
from pyteal.ast.unaryexpr import Len
from pyteal.ast.binaryexpr import ExtractUint16
from pyteal.ast.naryexpr import Concat

from pyteal.ast.abi.type import TypeSpec, BaseType, ComputedValue
from pyteal.ast.abi.tuple import encodeTuple
from pyteal.ast.abi.bool import Bool, BoolTypeSpec
from pyteal.ast.abi.uint import Uint16, Uint16TypeSpec
from pyteal.ast.abi.util import substringForDecoding

T = TypeVar("T", bound=BaseType)


class ArrayTypeSpec(TypeSpec, Generic[T]):
    """The abstract base class for both static and dynamic array TypeSpecs."""

    def __init__(self, value_type_spec: TypeSpec) -> None:
        super().__init__()
        self.value_spec: Final = value_type_spec

    def value_type_spec(self) -> TypeSpec:
        """Get the TypeSpec of the value type this array can hold."""
        return self.value_spec

    def storage_type(self) -> TealType:
        return TealType.bytes

    @abstractmethod
    def is_length_dynamic(self) -> bool:
        """Check if this array has a dynamic or static length."""
        pass

    def _stride(self) -> int:
        """Get the "stride" of this array.

        The stride is defined as the byte length of each element in the array's encoded "head"
        portion.

        If the underlying value type is static, then the stride is the static byte length of that
        type. Otherwise, the stride is the static byte length of a Uint16 (2 bytes).
        """
        if self.value_spec.is_dynamic():
            return Uint16TypeSpec().byte_length_static()
        return self.value_spec.byte_length_static()


ArrayTypeSpec.__module__ = "pyteal"


class Array(BaseType, Generic[T]):
    """The abstract base class for both ABI static and dynamic array instances.

    This class contains basic implementations of ABI array methods, including:
      * basic array elements setting method
      * basic encoding and decoding of ABI array
      * item retrieving by index (expression or integer)
    """

    def __init__(self, spec: ArrayTypeSpec) -> None:
        super().__init__(spec)

    def type_spec(self) -> ArrayTypeSpec[T]:
        return cast(ArrayTypeSpec, super().type_spec())

    def decode(
        self,
        encoded: Expr,
        *,
        startIndex: Expr = None,
        endIndex: Expr = None,
        length: Expr = None
    ) -> Expr:
        """Decode a substring of the passed in encoded byte string and set it as this type's value.

        Args:
            encoded: An expression containing the bytes to decode. Must evaluate to TealType.bytes.
            startIndex (optional): An expression containing the index to start decoding. Must
                evaluate to TealType.uint64. Defaults to None.
            endIndex (optional): An expression containing the index to stop decoding. Must evaluate
                to TealType.uint64. Defaults to None.
            length (optional): An expression containing the length of the substring to decode. Must
                evaluate to TealType.uint64. Defaults to None.

        Returns:
            An expression that partitions the needed parts from given byte strings and stores into
            the scratch variable.
        """
        extracted = substringForDecoding(
            encoded, startIndex=startIndex, endIndex=endIndex, length=length
        )
        return self.stored_value.store(extracted)

    def set(self, values: Sequence[T]) -> Expr:
        """Set the ABI array with a sequence of ABI type variables.

        The function first type-check the argument `values` to make sure the sequence of ABI type
        variables before storing them to the underlying ScratchVar. If any of the input element does
        not match expected array element type, error would be raised about type-mismatch.

        If static length of array is not available, this function would
        * infer the array length from the sequence element number.
        * store the inferred array length in uint16 format.
        * concatenate the encoded array length at the beginning of array encoding.

        Args:
            values: The sequence of ABI type variables to store in ABI array.

        Returns:
            A PyTeal expression that stores encoded sequence of ABI values in its internal
            ScratchVar.
        """
        for index, value in enumerate(values):
            if self.type_spec().value_type_spec() != value.type_spec():
                raise TealInputError(
                    "Cannot assign type {} at index {} to {}".format(
                        value.type_spec(),
                        index,
                        self.type_spec().value_type_spec(),
                    )
                )

        encoded = encodeTuple(values)

        if self.type_spec().is_length_dynamic():
            length_tmp = Uint16()
            length_prefix = Seq(length_tmp.set(len(values)), length_tmp.encode())
            encoded = Concat(length_prefix, encoded)

        return self.stored_value.store(encoded)

    def encode(self) -> Expr:
        """Encode the ABI array to be a byte string.

        Returns:
            A PyTeal expression that encodes this ABI array to a byte string.
        """
        return self.stored_value.load()

    @abstractmethod
    def length(self) -> Expr:
        """Get the element number of this ABI array.

        Returns:
            A PyTeal expression that represents the array length.
        """
        pass

    def __getitem__(self, index: Union[int, Expr]) -> "ArrayElement[T]":
        """Retrieve an ABI array element by an index (either a PyTeal expression or an integer).

        If the argument index is integer, the function will raise an error if the index is negative.

        Args:
            index: either an integer or a PyTeal expression that evaluates to a uint64.

        Returns:
            An ArrayElement that represents the ABI array element at the index.
        """
        if type(index) is int:
            if index < 0:
                raise TealInputError("Index out of bounds: {}".format(index))
            index = Int(index)
        return ArrayElement(self, cast(Expr, index))


Array.__module__ = "pyteal"


class ArrayElement(ComputedValue[T]):
    """The class that represents an ABI array element.

    This class requires a reference to the array that the array element belongs to, and a PyTeal
    expression (required to be TealType.uint64) which contains the array index.
    """

    def __init__(self, array: Array[T], index: Expr) -> None:
        """Creates a new ArrayElement.

        Args:
            array: The ABI array that the array element belongs to.
            index: A PyTeal expression (required to be TealType.uint64) stands for array index.
        """
        super().__init__()
        require_type(index, TealType.uint64)
        self.array = array
        self.index = index

    def produced_type_spec(self) -> TypeSpec:
        return self.array.type_spec().value_type_spec()

    def store_into(self, output: T) -> Expr:
        """Partitions the byte string of the given ABI array and stores the byte string of array
        element in the ABI value output.

        The function first checks if the output type matches with array element type, and throw
        error if type-mismatch.

        Args:
            output: An ABI typed value that the array element byte string stores into.

        Returns:
            An expression that stores the byte string of the array element into value `output`.
        """
        if output.type_spec() != self.produced_type_spec():
            raise TealInputError("Output type does not match value type")

        encodedArray = self.array.encode()
        arrayType = self.array.type_spec()

        # If the array element type is Bool, we compute the bit index
        # (if array is dynamic we add 16 to bit index for dynamic array length uint16 prefix)
        # and decode bit with given array encoding and the bit index for boolean bit.
        if output.type_spec() == BoolTypeSpec():
            bitIndex = self.index
            if arrayType.is_dynamic():
                bitIndex = bitIndex + Int(Uint16TypeSpec().bit_size())
            return cast(Bool, output).decodeBit(encodedArray, bitIndex)

        # Compute the byteIndex (first byte indicating the element encoding)
        # (If the array is dynamic, add 2 to byte index for dynamic array length uint16 prefix)
        byteIndex = Int(arrayType._stride()) * self.index
        if arrayType.is_length_dynamic():
            byteIndex = byteIndex + Int(Uint16TypeSpec().byte_length_static())

        arrayLength = self.array.length()

        # Handling case for array elements are dynamic:
        # * `byteIndex` is pointing at the uint16 byte encoding indicating the beginning offset of
        #   the array element byte encoding.
        #
        # * `valueStart` is extracted from the uint16 bytes pointed by `byteIndex`.
        #
        # * If `index == arrayLength - 1` (last element in array), `valueEnd` is pointing at the
        #   end of the array byte encoding.
        #
        # * otherwise, `valueEnd` is inferred from `nextValueStart`, which is the beginning offset
        #   of the next array element byte encoding.
        if arrayType.value_type_spec().is_dynamic():
            valueStart = ExtractUint16(encodedArray, byteIndex)
            nextValueStart = ExtractUint16(
                encodedArray, byteIndex + Int(Uint16TypeSpec().byte_length_static())
            )
            if arrayType.is_length_dynamic():
                valueStart = valueStart + Int(Uint16TypeSpec().byte_length_static())
                nextValueStart = nextValueStart + Int(
                    Uint16TypeSpec().byte_length_static()
                )

            valueEnd = (
                If(self.index + Int(1) == arrayLength)
                .Then(Len(encodedArray))
                .Else(nextValueStart)
            )

            return output.decode(encodedArray, startIndex=valueStart, endIndex=valueEnd)

        return output.decode(encodedArray, startIndex=byteIndex)


ArrayElement.__module__ = "pyteal"
