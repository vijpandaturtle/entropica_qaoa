"""
Different cost functions for VQE and one abstract template.
"""
from typing import Callable, Iterable, Union, List, Dict, Tuple
import warnings
import numpy as np
from collections import namedtuple
from copy import deepcopy

from pyquil.paulis import PauliSum, PauliTerm
from pyquil.quil import Program, Qubit, QubitPlaceholder, address_qubits
from pyquil.wavefunction import Wavefunction
from pyquil.api._wavefunction_simulator import WavefunctionSimulator
from pyquil.api._quantum_computer import QuantumComputer

from forest_qaoa.vqe.measurelib import (append_measure_register,
                                        sampling_expectation,
                                        commuting_decomposition,
                                        sampling_expectation_z_base)


LogEntry = namedtuple("LogEntry", ['x', 'fun'])


class AbstractCostFunction():
    """Template class for cost_functions that are passed to the optimizer

    Parameters
    ----------
    scalar_cost_function:
        If ``False``: self.__call__ has  signature
        ``(x, nshots) -> (exp_val, std_val)``
        If ``True``: ``self.__call__()`` has  signature ``(x) -> (exp_val)``,
        but the ``nshots`` argument in ``__init__`` has to be given.
    nshots:
        Optional.  Has to be given, if ``scalar_cost_function``
        is ``True``
        Number of shots to take for cost function evaluation.
    enable_logging:
        If true, a log is created which contains the parameter and function
        values at each function call. It is a list of namedtuples of the form
        ("x", "fun")

    Todo
    ----
    Remove return_standard_deviation argument and deprecation warnings.
    """

    def __init__(self,
                 return_standard_deviation: bool = False,
                 scalar_cost_function: bool = True,
                 nshots: int = None,
                 enable_logging: bool = False):
        """The constructor. See class docstring"""
        raise NotImplementedError()

    def __call__(self,
                 params: np.array,
                 nshots: int = None) -> Union[float, tuple]:
        """Estimate cost_functions(params) with nshots samples

        Parameters
        ----------
        params :
            Parameters of the state preparation circuit. Array of size `n`
            where `n` is the number of different parameters.
        nshots:
            Number of shots to take to estimate ``cost_function(params)``
            Has no effect, if ``__init__()`` was called with
            ``scalar_cost_function=True``

        Returns
        -------
        float or tuple (cost, cost_stdev)
            Either only the cost or a tuple of the cost and the standard
            deviation estimate based on the samples.

        """
        raise NotImplementedError()


# TODO support hamiltonians with qubit QubitPlaceholders?
class PrepareAndMeasureOnWFSim(AbstractCostFunction):
    """A cost function that prepares an ansatz and measures its energy w.r.t
    ``hamiltonian`` on the qvm

    Parameters
    ----------
    param prepare_ansatz:
        A parametric pyquil program for the state preparation
    param make_memory_map:
        A function that creates a memory map from the array of parameters
    hamiltonian:
        The hamiltonian with respect to which to measure the energy.
    sim:
        A WavefunctionSimulator instance to get the wavefunction from.
    scalar_cost_function:
        If True: __call__ has signature
        ``(x: array, nshots: int) -> (exp_val: float, std_dev: float)``
        If False: __call__ has signature ``(x: array) -> (exp_val: float)``
        i.e. it takes no argument ``nshots`` and only returns the expectation
        value
        Defaults to True.
    nshots:
        If ``scalar_cost_function=True`` is passed ``nshots`` has to be
        specified here.
    noisy:
        Add simulated noise to the energy? (the default is False)
    enable_logging:
        If true, a log is created which contains the parameter and function
        values at each function call. It is a list of namedtuples of the form
        ("x", "fun")
    qubit_mapping:
        A mapping to fix QubitPlaceholders to physical qubits. E.g.
        pyquil.quil.get_default_qubit_mapping(program) gives you on.

    Notes
    -----
    How to use the argument ``scalar_cost_function``:
    The constructor call to `PrepareAndMeasureOnWFSim` (or analogously
    `PrepareAndMeasureOnQVM`, `QAOACostFunctionOnWFSim`,
    `QAOACostFunctionOnQVM`)  turns from

    >>> cost_fun = PrepareAndMeasureOnWFSim(...,
                            return_standard_deviation=False,...)

    into

    >>> cost_fun = PrepareAndMeasureOnWFSim(..., scalar_cost_function=True,
                                                 nshots=<nshots>, ...)

    or if you want to return the standard deviation and vary `nshots` during
    the VQE run you have

    >>> cost_fun = PrepareAndMeasureOnWFSim(...,
                        return_standard_deviation=True,...)

    into

    >>> cost_fun = PrepareAndMeasureOnWFSim(...,
                        scalar_cost_function=False, ...)

    """

    def __init__(self,
                 prepare_ansatz: Program,
                 make_memory_map: Callable[[np.array], Dict],
                 hamiltonian: Union[PauliSum, np.array],
                 sim: WavefunctionSimulator,
                 return_standard_deviation: bool = None,
                 scalar_cost_function: bool = True,
                 nshots: int = None,
                 noisy: bool = False,
                 enable_logging: bool = False,
                 qubit_mapping: Dict[QubitPlaceholder,
                                     Union[Qubit, int]] = None):

        self.scalar = scalar_cost_function
        self.nshots = nshots
        if return_standard_deviation is not None:
            warnings.warn("The argument `return_standard_deviation` "
                          "is deprecated in favor of scalar_cost_function.\n"
                          "See the documentation for details of "
                          "vqe.cost_function.PrepareAndMeasureOnWFSim for "
                          "details, on how to update your code.\n"
                          " Proceeding now with scalar_cost_function = "
                          f"{True} and nshots = 1000.",
                          DeprecationWarning)
            self.scalar = True
            self.nshots = 1000

        if self.scalar and self.nshots is None:
            raise ValueError("If scalar_cost_function is set, nshots has to "
                             "be specified")

        self.make_memory_map = make_memory_map
        self.return_standard_deviation = return_standard_deviation
        self.noisy = noisy
        self.sim = sim  # TODO start own simulator, if None is passed

        # TODO automatically generate Qubit mapping, if None is passed?
        # TODO ask Rigetti to implement "<" between qubits?
        if qubit_mapping is not None:
            if isinstance(next(iter(qubit_mapping.values())), Qubit):
                int_mapping = dict(zip(qubit_mapping.keys(),
                                       [q.index for q in qubit_mapping.values()]))
            else:
                int_mapping = qubit_mapping
            self.prepare_ansatz = address_qubits(prepare_ansatz, qubit_mapping)
        else:
            int_mapping = None
            self.prepare_ansatz = prepare_ansatz

        # TODO What if prepare_ansatz acts on more qubits than ham?
        # then hamiltonian and wavefunction don't fit together...
        if isinstance(hamiltonian, PauliSum):
            nqubits = len(hamiltonian.get_qubits())
            self.ham = hamiltonian.matrix(int_mapping or {}, nqubits)
        elif isinstance(hamiltonian, (np.matrix, np.ndarray)):
            self.ham = hamiltonian
        else:
            raise ValueError(
                "hamiltonian has to be a PauliSum or numpy matrix")

        self.ham_squared = self.ham**2

        if enable_logging:
            self.log = []

    def __call__(self,
                 params: Union[list, np.ndarray],
                 nshots: int = None) -> Union[float, Tuple]:
        """Cost function that computes <psi|ham|psi> with psi prepared with
        prepare_ansatz(params).

        Parameters
        ----------
        params:
            Parameters of the state preparation circuit.
        nshots:
            Number of shots to take to estimate the energy (the default is 1000).

        Returns
        -------
        float or tuple (cost, cost_stdev)
            Either only the cost or a tuple of the cost and the standard
            deviation estimate based on the samples.
        """
        if nshots is None:
            if self.scalar:
                nshots = self.nshots
            else:
                raise ValueError("nshots cannot be None")

        memory_map = self.make_memory_map(params)
        wf = self.sim.wavefunction(self.prepare_ansatz, memory_map=memory_map)
        wf = np.reshape(wf.amplitudes, (-1, 1))
        E = np.conj(wf).T.dot(self.ham.dot(wf)).real
        sigma_E = nshots**(-1 / 2) * (
                    np.conj(wf).T.dot(self.ham_squared.dot(wf)).real - E**2)

        # add simulated noise, if wanted
        if self.noisy:
            E += np.random.randn() * sigma_E
        out = (float(E), float(sigma_E))  # Todo:Why the float casting?

        # Append function value and params to the log.
        # deepcopy is needed, because x may be a mutable type.
        try:
            self.log.append(LogEntry(x=deepcopy(params),
                                     fun=out))
        except AttributeError:
            pass

        # and return the expectation value or (exp_val, std_dev)
        if self.scalar:
            return out[0]
        else:
            return out

    def get_wavefunction(self,
                         params: Union[List, np.ndarray]) -> Wavefunction:
        """Same as __call__ but returns the wavefunction instead of cost

        Parameters
        ----------
        params:
            Parameters of the state preparation circuit

        Returns
        -------
        Wavefunction:
            The wavefunction prepared with parameters ``params``
        """
        memory_map = self.make_memory_map(params)
        wf = self.sim.wavefunction(self.prepare_ansatz, memory_map=memory_map)
        return wf


class PrepareAndMeasureOnQVM(AbstractCostFunction):
    """A cost function that prepares an ansatz and measures its energy w.r.t
    hamiltonian on a quantum computer (or simulator).

    This cost_function makes use of pyquils parametric circuits and thus
    has to be supplied with a parametric circuit and a function to create
    memory maps that can be passed to qvm.run.

    Parameters
    ----------
    prepare_ansatz:
        A parametric pyquil program for the state preparation
    make_memory_map:
        A function that creates a memory map from the array of parameters
    hamiltonian:
        The hamiltonian
    qvm:
        Connection the QC to run the program on.
    scalar_cost_function:
        If True: __call__ has signature
        ``(x: array, nshots: int) -> (exp_val: float, std_dev: float)``
        If False: __call__ has signature ``(x: array) -> (exp_val: float)``
        i.e. it takes no argument ``nshots`` and only returns the expectation
        value
        Defaults to True.
    nshots:
        If ``scalar_cost_function=True`` is passed ``nshots`` has to be
        specified here.
    base_numshots:
        numshots multiplier to compile into the binary. The argument nshots of
         __call__ is then a multplier of this.
    qubit_mapping:
        A mapping to fix all QubitPlaceholders to physical qubits. E.g.
        pyquil.quil.get_default_qubit_mapping(program) gives you on.
    enable_logging:
        If true, a log is created which contains the parameter and function
        values at each function call. It is a list of namedtuples of the form
        ("x", "fun")
    """

    def __init__(self,
                 prepare_ansatz: Program,
                 make_memory_map: Callable[[Iterable], dict],
                 hamiltonian: PauliSum,
                 qvm: QuantumComputer,
                 return_standard_deviation: bool = None,
                 scalar_cost_function: bool = True,
                 nshots: int = None,
                 base_numshots: int = 100,
                 qubit_mapping: Dict[QubitPlaceholder, Union[Qubit, int]] = None,
                 enable_logging: bool = False):


        self.scalar = scalar_cost_function
        self.nshots = nshots
        if self.scalar and self.nshots is None:
            raise ValueError("If scalar_cost_function is set, nshots has to "
                             "be specified")

        if return_standard_deviation is not None:
            warnings.warn("The argument `return_standard_deviation` "
                          "is deprecated in favor of scalar_cost_function.\n"
                          "See the documentation for details of "
                          "vqe.cost_function.PrepareAndMeasureOnWFSim for "
                          "details, on how to update your code.\n"
                          " Proceeding now with scalar_cost_function = "
                          f"{True} and nshots = 1000.",
                          DeprecationWarning)
            self.scalar = True
            self.nshots = 1000

        self.qvm = qvm
        self.return_standard_deviation = return_standard_deviation
        self.make_memory_map = make_memory_map

        if qubit_mapping is not None:
            prepare_ansatz = address_qubits(prepare_ansatz, qubit_mapping)
            ham = address_qubits_hamiltonian(hamiltonian, qubit_mapping)
        else:
            ham = hamiltonian

        self.hams = commuting_decomposition(ham)
        self.exes = []
        for ham in self.hams:
            # need a different program for each of the self commuting hams
            p = prepare_ansatz.copy()
            append_measure_register(p,
                                    qubits=ham.get_qubits(),
                                    trials=base_numshots,
                                    ham=ham)
            self.exes.append(qvm.compile(p))

        if enable_logging:
            self.log = []

    def __call__(self,
                 params: np.array,
                 nshots: int = None) -> Union[float, Tuple]:
        """
        Parameters
        ----------
        param params:
            the parameters to run the state preparation circuit with
        param N:
            Number of times to run exe

        Returns
        -------
        float or tuple (cost, cost_stdev)
            Either only the cost or a tuple of the cost and the standard
            deviation estimate based on the samples.
        """
        if nshots is None:
            if self.scalar:
                nshots = self.nshots
            else:
                raise ValueError("nshots cannot be None")

        memory_map = self.make_memory_map(params)

        bitstrings = []
        for exe in self.exes:
            bitstring = self.qvm.run(exe, memory_map=memory_map)
            for i in range(nshots - 1):
                new_bits = self.qvm.run(exe, memory_map=memory_map)
                bitstring = np.append(bitstring, new_bits, axis=0)
            bitstrings.append(bitstring)

        out = sampling_expectation_z_base(self.hams, bitstrings)

        # Append function value and params to the log.
        # deepcopy is needed, because x may be a mutable type.
        try:
            self.log.append(LogEntry(x=deepcopy(params),
                                     fun=out))
        except AttributeError:
            pass

        if self.scalar:
            return out[0]
        else:
            return out


def address_qubits_hamiltonian(hamiltonian: PauliSum,
                               qubit_mapping: Dict[QubitPlaceholder, Union[Qubit, int]]) -> PauliSum:
    """Map Qubit Placeholders to ints in a PauliSum.

    Parameters
    ----------
    hamiltonian:
        The PauliSum.
    qubit_mapping:
        A qubit_mapping. e.g. provided by pyquil.quil.get_default_qubit_mapping.

    Returns
    -------
    PauliSum
        A PauliSum with remapped Qubits.

    Note
    ----
    This code relies completely on going all the way down the rabbits hole
    with for loops. It would be preferable to have this functionality in
    pyquil.paulis.PauliSum directly
    """
    out = PauliTerm("I", 0, 0)
    # Make sure we map to integers and not to Qubits(), these are not
    # supported by pyquil.paulis.PauliSum().
    if set([Qubit]) == set(map(type, qubit_mapping.values())):
        qubit_mapping = dict(zip(qubit_mapping.keys(),
                                 [q.index for q in qubit_mapping.values()]))
    # And change all of them
    for term in hamiltonian:
        coeff = term.coefficient
        ops = []
        for factor in term:
            ops.append((factor[1], qubit_mapping[factor[0]]))
        out += PauliTerm.from_list(ops, coeff)
    return out
