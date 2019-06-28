import numpy as np

from pyquil.paulis import PauliSum, PauliTerm
from pyquil.api import WavefunctionSimulator, local_qvm, get_qc
from pyquil.quil import Program, get_default_qubit_mapping
from pyquil.gates import RX, CNOT
from pyquil.quil import QubitPlaceholder, Qubit, address_qubits

from vqe.optimizer import scipy_optimizer
from vqe.cost_function import PrepareAndMeasureOnWFSim, PrepareAndMeasureOnQVM

# gonna need this program and hamiltonian for both tests.
# So define them globally
q0 = QubitPlaceholder()
q1 = QubitPlaceholder()
hamiltonian = PauliTerm("Z", q0, 2.5)
hamiltonian += PauliTerm("Z", q1, 0.5)
hamiltonian += PauliTerm("Z", q1, -1) * PauliTerm("Z", q0)


prepare_ansatz = Program()
params = prepare_ansatz.declare("params", memory_type="REAL", memory_size=4)
prepare_ansatz.inst(RX(params[0], q0))
prepare_ansatz.inst(RX(params[1], q1))
prepare_ansatz.inst(CNOT(q0, q1))
prepare_ansatz.inst(RX(params[2], q0))
prepare_ansatz.inst(RX(params[3], q1))

p0 = [0, 5.2, 0, 0]


def test_vqe_on_WFSim_QubitPlaceholders():
    qubit_mapping = get_default_qubit_mapping(prepare_ansatz)
    log = []
    sim = WavefunctionSimulator()
    cost_fun = PrepareAndMeasureOnWFSim(prepare_ansatz=prepare_ansatz,
                                        make_memory_map=lambda p: {"params": p},
                                        hamiltonian=hamiltonian,
                                        sim=sim,
                                        return_standard_deviation=True,
                                        noisy=False,
                                        log=log,
                                        qubit_mapping=qubit_mapping)

    with local_qvm():
        out = scipy_optimizer(cost_fun, p0, epsilon=1e-3)
        print(out)
        prog = address_qubits(prepare_ansatz, qubit_mapping=qubit_mapping)
        wf = sim.wavefunction(prog, {"params": out['x']})
    print(wf.probabilities())
    assert np.allclose(np.abs(wf.amplitudes**2), [0, 0, 0, 1], rtol=1.5, atol=0.01)
    assert np.allclose(out['fun'], -4)
    assert out['success']


def test_vqe_on_QVM_QubitPlaceholders():
    qubit_mapping = {q0: 0, q1: 1}
    p0 = [3.1, -1.5, 0, 0]  # make it easier when sampling
    log = []
    qvm = get_qc("2q-qvm")
    with local_qvm():
        cost_fun = PrepareAndMeasureOnQVM(prepare_ansatz=prepare_ansatz,
                                          make_memory_map=lambda p: {"params": p},
                                          hamiltonian=hamiltonian,
                                          qvm=qvm,
                                          return_standard_deviation=True,
                                          base_numshots=50,
                                          log=log,
                                          qubit_mapping=qubit_mapping)
        out = scipy_optimizer(cost_fun, p0, epsilon=1e-2, nshots=4)
        print(out)
    assert np.allclose(out['fun'], -4, rtol=1.1)
    assert out['success']
