# Copyright 2018 D-Wave Systems Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import collections
import unittest.mock as mock
from uuid import uuid4

import dimod
import dwave_networkx as dnx
from tabu import TabuSampler
import dwave.cloud.computation

try:
    from neal import SimulatedAnnealingSampler
except ImportError:
    from dimod import SimulatedAnnealingSampler

import concurrent.futures
import numpy as np


C4 = dnx.chimera_graph(4, 4, 4)


class MockDWaveSampler(dimod.Sampler, dimod.Structured):
    """Mock sampler modeled after DWaveSampler that can be used for tests."""

    nodelist = None
    edgelist = None
    properties = None
    parameters = None

    def __init__(self, broken_nodes=None, **config):
        if broken_nodes is None:
            self.nodelist = sorted(C4.nodes)
            self.edgelist = sorted(tuple(sorted(edge)) for edge in C4.edges)
        else:
            self.nodelist = sorted(v for v in C4.nodes if v not in broken_nodes)
            self.edgelist = sorted(tuple(sorted((u, v))) for u, v in C4.edges
                                   if u not in broken_nodes and v not in broken_nodes)

        # mark the sample kwargs
        self.parameters = parameters = {}
        parameters['num_reads'] = ['num_reads_range']
        parameters['flux_biases'] = ['j_range']
        parameters['label'] = []

        # add the interesting properties manually
        self.properties = properties = {}
        properties['j_range'] = [-1.0, 1.0]
        properties['h_range'] = [-2.0, 2.0]
        properties['num_reads_range'] = [1, 10000]
        properties['num_qubits'] = len(C4)
        properties['category'] = 'qpu'
        properties['quota_conversion_rate'] = 1
        properties['topology'] = {'type': 'chimera', 'shape': [4, 4, 4]}
        properties['chip_id'] = 'MockDWaveSampler'
        properties['annealing_time_range'] = [1, 2000]
        properties['num_qubits'] = len(self.nodelist)
        properties['extended_j_range'] = [-2.0, 1.0]
        properties["supported_problem_types"] = ['ising', 'qubo']
        # add some occasionally useful properties
        properties["default_annealing_time"] = 20
        properties["default_programming_thermalization"] = 1000
        properties["default_readout_thermalization"] = 0
        properties["h_gain_schedule_range"] = [-4.0, 4.0]
        properties["max_anneal_schedule_points"] = 12
        properties["max_h_gain_schedule_points"] = 20
        properties["per_qubit_coupling_range"] = [-18.0, 15.0]
        properties["problem_run_duration_range"] = [0, 1000000]
        properties["programming_thermalization_range"] = [0, 10000]
        properties["readout_thermalization_range"] = [0, 10000]

    @dimod.bqm_structured
    def sample(self, bqm, num_reads=10, flux_biases=[], **kwargs):
        # we are altering the bqm if flux_biases given

        info = dict(problem_id=str(uuid4()))
        label = kwargs.get('label')
        if label is not None:
            info.update(problem_label=label)

        if not flux_biases:
            ss = SimulatedAnnealingSampler().sample(bqm, num_reads=num_reads)
            ss.info.update(info)
            return ss

        new_bqm = bqm.copy()

        for v, fbo in enumerate(flux_biases):
            self.flux_biases_flag = True
            new_bqm.add_variable(v, 1000. * fbo)  # add the bias

        response = SimulatedAnnealingSampler().sample(new_bqm, num_reads=num_reads)

        # recalculate the energies with the old bqm
        return dimod.SampleSet.from_samples_bqm([{v: sample[v] for v in bqm.variables}
                                                 for sample in response.samples()],
                                                bqm, info=info)

class MockLeapHybridDQMSampler:
    """Mock sampler modeled after LeapHybridDQMSampler that can be used for tests."""
    def __init__(self, **config):
        self.parameters = {'time_limit': ['parameters'],
                           'label': []}

        self.properties = {'category': 'hybrid',
                           'supported_problem_types': ['dqm'],
                           'quota_conversion_rate': 20,
                           'minimum_time_limit': [[20000, 5.0],
                                                  [100000, 6.0],
                                                  [200000, 13.0],
                                                  [500000, 34.0],
                                                  [1000000, 71.0],
                                                  [2000000, 152.0],
                                                  [5000000, 250.0],
                                                  [20000000, 400.0],
                                                  [250000000, 1200.0]],
                           'maximum_time_limit_hrs': 24.0,
                           'maximum_number_of_variables': 3000,
                           'maximum_number_of_biases': 3000000000}

    def sample_dqm(self, dqm, **kwargs):
        num_samples = 12    # min num of samples from dqm solver
        samples = np.empty((num_samples, dqm.num_variables()), dtype=int)

        for vi, v in enumerate(dqm.variables):
            samples[:, vi] = np.random.choice(dqm.num_cases(v), size=num_samples)

        return dimod.SampleSet.from_samples((samples, dqm.variables),
                                             vartype='DISCRETE',
                                             energy=dqm.energies(samples))

    def min_time_limit(self, dqm):
        # not caring about the problem, just returning the min
        return self.properties['minimum_time_limit'][0][1]

class MockLeapHybridSolver:

    properties = {'supported_problem_types': ['bqm'],
                  'minimum_time_limit': [[1, 1.0], [1024, 1.0],
                                         [4096, 10.0], [10000, 40.0]],
                  'parameters': {'time_limit': None},
                  'category': 'hybrid',
                  'quota_conversion_rate': 1}

    supported_problem_types = ['bqm']

    def upload_bqm(self, bqm, **parameters):
        bqm_adjarray = dimod.serialization.fileview.load(bqm)
        future = concurrent.futures.Future()
        future.set_result(bqm_adjarray)
        return future

    def sample_bqm(self, sapi_problem_id, time_limit):
        #Workaround until TabuSampler supports C BQMs
        bqm = dimod.BQM(sapi_problem_id.linear,
                                    sapi_problem_id.quadratic,
                                    sapi_problem_id.offset,
                                    sapi_problem_id.vartype)
        result = TabuSampler().sample(bqm, timeout=1000*int(time_limit))
        future = dwave.cloud.computation.Future('fake_solver', None)
        future._result = {'sampleset': result, 'problem_type': 'bqm'}
        return future
