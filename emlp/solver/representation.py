import numpy as np
#import torch
import jax
from jax import jit
from functools import partial
import jax.numpy as jnp
from jax import device_put
import optax
import collections,itertools
from functools import lru_cache as cache
from .utils import ltqdm,prod
from .linear_operator_jax import LinearOperator,Lazy
from .linear_operators import ConcatLazy,I,lazify
import scipy as sp
import scipy.linalg
import functools
import random
import logging
import math
from jax.ops import index, index_add, index_update
import matplotlib.pyplot as plt
from collections import Counter
from functools import reduce
import emlp.solver
import numpy as np
import matplotlib.pyplot as plt
from sklearn.cluster import KMeans
from tqdm.auto import tqdm
#TODO: add rep,v = flatten({'Scalar':..., 'Vector':...,}), to_dict(rep,vector) returns {'Scalar':..., 'Vector':...,}
#TODO and simpler rep = flatten({Scalar:2,Vector:10,...}),
# Do we even want + operator to implement non canonical orderings?
class OrderedCounter(collections.Counter,collections.OrderedDict): pass

class Rep(object):
    concrete=True
    def __eq__(self, other): raise NotImplementedError
    def size(self): raise NotImplementedError # dim(V) dimension of the representation
    
    @property
    def T(self): raise NotImplementedError # dual representation V*, rho*, drho*
    def __repr__(self): return str(self)#raise NotImplementedError
    def __str__(self): raise NotImplementedError 
    def __call__(self,G): 
        print(type(self),self)
        raise NotImplementedError # set the symmetry group
    def canonicalize(self): return self, np.arange(self.size()) # return canonicalized rep
    def size(self):
        print(self,type(self))
        raise NotImplementedError # The dimension of the representation
    def rho_dense(self,M):
        rho = self.rho(M)
        return rho.to_dense() if isinstance(rho,LinearOperator) else rho
    def drho_dense(self,A):
        rho = self.drho(M)
        return drho.to_dense() if isinstance(drho,LinearOperator) else drho
    
    def constraint_matrix(self):
        """ Given a sequence of exponential generators [A1,A2,...]
        and a tensor rank (p,q), the function concatenates the representations
        [drho(A1), drho(A2), ...] into a single large constraint matrix C.
        Input: [generators seq(tensor(d,d))], [rank tuple(p,q)], [d int] """
        n = self.size()
        constraints = []
        constraints.extend([lazify(self.rho(h))-I(n) for h in self.G.discrete_generators])
        constraints.extend([lazify(self.drho(A)) for A in self.G.lie_algebra])
        return ConcatLazy(constraints) if constraints else lazify(jnp.zeros(1,n))

    #@disk_cache('./_subspace_cache_jax.dat')
    solcache = {}
    def symmetric_basis(self):  
        """ Given an array of generators [M1,M2,...] and tensor rank (p,q)
            this function computes the orthogonal complement to the projection
            matrix formed by stacking the rows of drho(Mi) together.
            Output [Q (d^(p+q),r)] """
        if self==Scalar: return jnp.ones((1,1))
        canon_rep,perm = self.canonicalize()
        invperm = np.argsort(perm)
        if canon_rep not in self.solcache:
            logging.info(f"{canon_rep} cache miss")
            logging.info(f"Solving basis for {self}"+(f", for G={self.G}" if hasattr(self,"G") else ""))
            #if isinstance(group,Trivial): return np.eye(size(rank,group.d))
            C_lazy = canon_rep.constraint_matrix()
            if prod(C_lazy.shape)>3e7: #Too large to use SVD
                result = krylov_constraint_solve(C_lazy)
            else:
                C_dense = C_lazy.to_dense()
                result = orthogonal_complement(C_dense)
            self.solcache[canon_rep]=result
        return self.solcache[canon_rep][invperm]
    
    def symmetric_projector(self):
        Q = self.symmetric_basis()
        Q_lazy = lazify(Q)
        P = Q_lazy@Q_lazy.H
        return P

    def __add__(self, other): # Tensor sum representation R1 + R2
        if isinstance(other,int):
            if other==0: return self
        elif all(rep.concrete for rep in (self,other) if hasattr(rep,'concrete')):
            return emlp.solver.product_sum_reps.SumRep(self,other)
        else:
            return emlp.solver.product_sum_reps.DeferredSumRep(self,other)
    def __radd__(self,other):
        if isinstance(other,int): 
            if other==0: return self
        else: return NotImplemented
        
    def __mul__(self,other):
        if isinstance(other,(int,ScalarRep)):
            if other==1 or other==Scalar: return self
            if other==0: return 0
            if (not hasattr(self,'concrete')) or self.concrete:
                return emlp.solver.product_sum_reps.SumRep(*(other*[self]))
            else:
                return emlp.solver.product_sum_reps.DeferredSumRep(*(other*[self]))
            return sum(other*[self])
        elif all(rep.concrete for rep in (self,other) if hasattr(rep,'concrete')):
            if any(isinstance(rep,emlp.solver.product_sum_reps.SumRep) for rep in [self,other]):
                return emlp.solver.product_sum_reps.distribute_product([self,other])
            elif (hasattr(self,'G') and hasattr(other,'G') and self.G!=other.G) or not (hasattr(self,'G') and hasattr(other,'G')):
                return emlp.solver.product_sum_reps.DirectProduct(self,other)
            else: 
                return emlp.solver.product_sum_reps.ProductRep(self,other)
        else:
            return emlp.solver.product_sum_reps.DeferredProductRep(self,other)
            
    def __rmul__(self,other):
        if isinstance(other,(int,ScalarRep)): 
            if other==1 or other==Scalar: return self
            if other==0: return 0
            if (not hasattr(self,'concrete')) or self.concrete:
                return emlp.solver.product_sum_reps.SumRep(*(other*[self]))
            else:
                return emlp.solver.product_sum_reps.DeferredSumRep(*(other*[self]))
        else: return NotImplemented

    def __pow__(self,other):
        assert isinstance(other,int), f"Power only supported for integers, not {type(other)}"
        assert other>=0, f"Negative powers {other} not supported"
        return reduce(lambda a,b:a*b,other*[self],Scalar)
    def __rshift__(self,other):
        return other*self.T
    def __lshift__(self,other):
        return self*other.T
    def __lt__(self, other):
        #Canonical ordering is determined 1st by Group, then by size, then by hash
        if other==Scalar: return False
        try: 
            if self.G<other.G: return True
            if self.G>other.G: return False
        except (AttributeError,TypeError): pass
        if self.size()<other.size(): return True
        if self.size()>other.size(): return False
        return hash(self) < hash(other) #For sorting purposes only
    def __mod__(self,other): # Wreath product
        raise NotImplementedError

# A possible
class ScalarRep(Rep):
    def __init__(self,G=None):
        self.G=G
        self.concrete = True#(G is not None)
        self.is_regular = True
    def __call__(self,G):
        self.G=G
        return self
    def size(self):
        return 1
    def __repr__(self): return str(self)#f"T{self.rank+(self.G,)}"
    def __str__(self):
        return "V⁰"
    @property
    def T(self):
        return self
    def rho(self,M):
        return jnp.eye(1)
    def drho(self,M):
        return 0*jnp.eye(1)
    def __hash__(self):
        return 0
    def __eq__(self,other):
        return isinstance(other,ScalarRep)
    def __mul__(self,other):
        if isinstance(other,int): return super().__mul__(other)
        return other
    def __rmul__(self,other):
        if isinstance(other,int): return super().__rmul__(other)
        return other

class Base(Rep):
    def __init__(self,G=None):
        self.G=G
        self.concrete = (G is not None)
        if G is not None: self.is_regular = G.is_regular
    def __call__(self,G):
        return self.__class__(G)
    def rho(self,M):
        if hasattr(self,'G') and isinstance(M,dict): M=M[self.G]
        return M
    def drho(self,A):
        if hasattr(self,'G') and isinstance(A,dict): A=A[self.G]
        return A
    def size(self):
        assert self.G is not None, f"must know G to find size for rep={self}"
        return self.G.d
    def __repr__(self): return str(self)#f"T{self.rank+(self.G,)}"
    def __str__(self):
        return "V"# +(f"_{self.G}" if self.G is not None else "")
    @property
    def T(self):
        return Dual(self.G)
    def __hash__(self):
        return hash((type(self),self.G))
    def __eq__(self,other):
        return type(other)==type(self) and self.G==other.G
    def __lt__(self,other):
        if isinstance(other,Dual): return True
        return super().__lt__(other)

class Dual(Base):
    def __new__(cls,G=None):
        if G is not None and G.is_orthogonal: return Base(G)
        else: return super(Dual,cls).__new__(cls)
    def rho(self,M):
        MinvT = M.invT() if hasattr(M,'invT') else jnp.linalg.inv(M).T
        return MinvT
    def drho(self,A):
        return -A.T
    def __str__(self):
        return "V*"#+(f"_{self.G}" if self.G is not None else "")
    @property
    def T(self):
        return Base(self.G)
    def __lt__(self,other):
        if isinstance(other,Base): return False
        return super().__lt__(other)

V=Vector= Base()
Scalar = ScalarRep()#V**0
def T(p,q=0,G=None):
    return (V**p*V.T**q)(G)


def orthogonal_complement(proj):
    """ Computes the orthogonal complement to a given matrix proj"""
    U,S,VH = jnp.linalg.svd(proj,full_matrices=True) # Changed from full_matrices=True
    rank = (S>1e-5).sum()
    return VH[rank:].conj().T

def krylov_constraint_solve(C,tol=1e-5):
    r = 5
    if C.shape[0]*r*2>2e9: raise Exception(f"Solns for contraints {C.shape} too large to fit in memory")
    found_rank=5
    while found_rank==r:
        r *= 2
        if C.shape[0]*r>2e9:
            logging.error(f"Hit memory limits, switching to sample equivariant subspace of size {found_rank}")
            break
        Q = krylov_constraint_solve_upto_r(C,r,tol)
        found_rank = Q.shape[-1]
    return Q

def krylov_constraint_solve_upto_r(C,r,tol=1e-5,lr=1e-2):#,W0=None):
    W = np.random.randn(C.shape[-1],r)/np.sqrt(C.shape[-1])# if W0 is None else W0
    W = device_put(W)
    opt_init,opt_update = optax.sgd(lr,.9)
    opt_state = opt_init(W)  # init stats

    def loss(W):
        return (jnp.absolute(C@W)**2).sum()/2 # added absolute for complex support

    loss_and_grad = jit(jax.value_and_grad(loss))
    # setup progress bar
    pbar = tqdm(total=100,desc=f'Krylov Solving for Equivariant Subspace r<={r}',
    bar_format="{l_bar}{bar}| {n:.3g}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]")
    prog_val = 0
    lstart, _ = loss_and_grad(W)
    
    for i in range(20000):
        
        lossval, grad = loss_and_grad(W)
        updates, opt_state = opt_update(grad, opt_state, W)
        W = optax.apply_updates(W, updates)
        # update progress bar
        progress = max(100*np.log(lossval/lstart)/np.log(tol**2/lstart)-prog_val,0)
        progress = min(100-prog_val,progress)
        if progress>0:
            prog_val += progress
            pbar.update(progress)

        if jnp.sqrt(lossval) <tol: # check convergence condition
            pbar.close()
            break # has converged
        if lossval>2e3 and i>100: # Solve diverged due to too high learning rate
            logging.warning(f"Constraint solving diverged, trying lower learning rate {lr/3:.2e}")
            if lr < 1e-4: raise ConvergenceError(f"Failed to converge even with smaller learning rate {lr:.2e}")
            return krylov_constraint_solve_upto_r(C,r,tol,lr=lr/3)
    else: raise ConvergenceError("Failed to converge.")
    # Orthogonalize solution at the end
    U,S,VT = np.linalg.svd(np.array(W),full_matrices=False) 
    # Would like to do economy SVD here (to not have the unecessary O(n^2) memory cost) 
    # but this is not supported in numpy (or Jax) unfortunately.
    rank = (S>10*tol).sum()
    Q = device_put(U[:,:rank])
    # final_L
    final_L = loss_and_grad(Q)[0]
    assert final_L <tol, f"Normalized basis has too high error {final_L:.2e} for tol {tol:.2e}"
    scutoff = (S[rank] if r>rank else 0)
    assert rank==0 or scutoff < S[rank-1]/100, f"Singular value gap too small: {S[rank-1]:.2e} \
        above cutoff {scutoff:.2e} below cutoff. Final L {final_L:.2e}, earlier {S[rank-5:rank]}"
    #logging.debug(f"found Rank {r}, above cutoff {S[rank-1]:.3e} after {S[rank] if r>rank else np.inf:.3e}. Loss {final_L:.1e}")
    return Q

class ConvergenceError(Exception): pass

def sparsify_basis(Q,lr=1e-2): #(n,r)
    W = np.random.randn(Q.shape[-1],Q.shape[-1])
    W,_ = np.linalg.qr(W)
    W = device_put(W.astype(jnp.float32))
    opt_init,opt_update = optax.adam(lr)#optax.sgd(1e2,.9)#optax.adam(lr)#optax.sgd(3e-3,.9)#optax.adam(lr)
    opt_update = jit(opt_update)
    opt_state = opt_init(W)  # init stats

    def loss(W):
        return jnp.abs(Q@W.T).mean() + .1*(jnp.abs(W.T@W-jnp.eye(W.shape[0]))).mean()+.01*jax.numpy.linalg.slogdet(W)[1]**2

    loss_and_grad = jit(jax.value_and_grad(loss))

    for i in ltqdm(range(3000),desc=f'sparsifying basis',level='info'):
        lossval, grad = loss_and_grad(W)
        updates, opt_state = opt_update(grad, opt_state, W)
        W = optax.apply_updates(W, updates)
        #W,_ = np.linalg.qr(W)
        if lossval>1e2 and i>100: # Solve diverged due to too high learning rate
            logging.warning(f"basis sparsification diverged, trying lower learning rate {lr/3:.2e}")
            return sparsify_basis(Q,lr=lr/3)
    Q = np.copy(Q@W.T)
    Q[np.abs(Q)<1e-2]=0
    Q[np.abs(Q)>1e-2] /= np.abs(Q[np.abs(Q)>1e-2])
    A = Q@(1+np.arange(Q.shape[-1]))
    if len(np.unique(np.abs(A)))!=Q.shape[-1]+1 and len(np.unique(np.abs(A)))!=Q.shape[-1]:
        logging.error(f"Basis elems did not separate: found only {len(np.unique(np.abs(A)))}/{Q.shape[-1]}")
        #raise ConvergenceError(f"Basis elems did not separate: found only {len(np.unique(A))}/{Q.shape[-1]}")
    return Q

#@partial(jit,static_argnums=(0,1))
def bilinear_weights(out_rep,in_rep):
    W_rep,W_perm = (in_rep>>out_rep).canonicalize()
    inv_perm = np.argsort(W_perm)
    mat_shape = out_rep.size(),in_rep.size()
    x_rep=in_rep
    W_multiplicities = W_rep.reps
    x_multiplicities = x_rep.reps
    x_multiplicities = {rep:n for rep,n in x_multiplicities.items() if rep!=Scalar}
    nelems = lambda nx,rep: min(nx,rep.size())
    active_dims = sum([W_multiplicities.get(rep,0)*nelems(n,rep) for rep,n in x_multiplicities.items()])
    reduced_indices_dict = {rep:ids[np.random.choice(len(ids),nelems(len(ids),rep))].reshape(-1)\
                                for rep,ids in x_rep.as_dict(np.arange(x_rep.size())).items()}
    # Apply the projections for each rank, concatenate, and permute back to orig rank order
    @jit
    def lazy_projection(params,x): # (r,), (*c) #TODO: find out why backwards of this function is so slow
        bshape = x.shape[:-1]
        x = x.reshape(-1,x.shape[-1])
        bs = x.shape[0]
        i=0
        Ws = []
        for rep, W_mult in W_multiplicities.items():
            if rep not in x_multiplicities:
                Ws.append(jnp.zeros((bs,W_mult*rep.size())))
                continue
            x_mult = x_multiplicities[rep]
            n = nelems(x_mult,rep)
            i_end = i+W_mult*n
            bids =  reduced_indices_dict[rep]
            bilinear_params = params[i:i_end].reshape(W_mult,n) # bs,nK-> (nK,bs)
            i = i_end  # (bs,W_mult,d^r) = (W_mult,n)@(n,d^r,bs)
            bilinear_elems = bilinear_params@x[...,bids].T.reshape(n,rep.size()*bs)
            bilinear_elems = bilinear_elems.reshape(W_mult*rep.size(),bs).T
            Ws.append(bilinear_elems)
        Ws = jnp.concatenate(Ws,axis=-1) #concatenate over rep axis
        return Ws[...,inv_perm].reshape(*bshape,*mat_shape) # reorder to original rank ordering
    return active_dims,lazy_projection
        
@jit
def mul_part(bparams,x,bids):
    b = prod(x.shape[:-1])
    return (bparams@x[...,bids].T.reshape(bparams.shape[-1],-1)).reshape(-1,b).T


def vis(repin,repout,cluster=True):
    rep = (repin>>repout)
    P = rep.symmetric_projector() # compute the equivariant basis
    Q = rep.symmetric_basis()
    v = np.random.randn(P.shape[1])  # sample random vector
    v = P@v                         # project onto equivariant subspace
    if cluster: # cluster nearby values for better color separation in plot
        v = KMeans(n_clusters=Q.shape[-1]).fit(v.reshape(-1,1)).labels_
    plt.imshow(v.reshape(repout.size(),repin.size()))
    plt.axis('off')


import emlp.solver.groups # Why is this necessary to avoid circular import?