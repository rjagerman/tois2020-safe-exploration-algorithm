import numpy as np
import numba
from experiments.classification.policies.util import argmax, init_weights
from experiments.classification.policies.greedy import GreedyPolicy
from experiments.classification.policies.uniform import UniformPolicy


@numba.jitclass([
    ('k', numba.int32),
    ('d', numba.int32),
    ('lr', numba.float64),
    ('l2', numba.float64),
    ('eps', numba.float64),
    ('w', numba.float64[:,:])
])
class _EpsgreedyPolicy:
    def __init__(self, k, d, lr, l2, eps, w):
        self.k = k
        self.d = d
        self.lr = lr
        self.l2 = l2
        self.eps = eps
        self.w = w
    
    def update(self, dataset, index, a, r):
        x, _ = dataset.get(index)
        s = x.dot(self.w[:, a])
        loss = s - r # square loss reward compared to score (predicted reward)
        for i in range(x.nnz):
            col = x.indices[i]
            val = x.data[i]
            self.w[col, a] -= self.lr * (val * loss + self.l2 * self.w[col, a])
    
    def draw(self, x):
        if np.random.random() < self.eps:
            return np.random.randint(self.k)
        else:
            return self.max(x)
    
    def max(self, x):
        return argmax(x.dot(self.w))
    
    def probability(self, x, a):
        s = x.dot(self.w)
        up = 1.0 / float(self.k)
        m = np.max(s)
        gp = 1.0 * (s == m)
        gp /= np.sum(gp)
        gp = gp[a]
        return (self.eps) * up + (1 - self.eps) * gp


def __getstate(self):
    return {
        'k': self.k,
        'd': self.d,
        'lr': self.lr,
        'l2': self.l2,
        'eps': self.eps,
        'w': self.w
    }


def __setstate(self, state):
    self.k = state['k']
    self.d = state['d']
    self.lr = state['lr']
    self.l2 = state['l2']
    self.eps = state['eps']
    self.w = state['w']


def __reduce(self):
    return (EpsgreedyPolicy, (self.k, self.d), self.__getstate__())


def __deepcopy(self):
    return EpsgreedyPolicy(self.k, self.d, self.lr, self.l2, self.eps, np.copy(self.w))


def EpsgreedyPolicy(k, d, lr=0.01, l2=0.0, eps=0.05, w=None, **kw_args):
    w = init_weights(k, d, w)
    out = _EpsgreedyPolicy(k, d, lr, l2, eps, w)
    setattr(out.__class__, '__getstate__', __getstate)
    setattr(out.__class__, '__setstate__', __setstate)
    setattr(out.__class__, '__reduce__', __reduce)
    setattr(out.__class__, '__deepcopy__', __deepcopy)
    return out
