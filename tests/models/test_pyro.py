import os

import numpy as np
import pyro
import pyro.distributions as dist
import torch
import torch.nn as nn
from anndata import AnnData
from pyro.infer.autoguide import AutoNormal, init_to_mean
from pyro.nn import PyroModule, PyroSample
from scipy.sparse import issparse

from scvi import _CONSTANTS
from scvi.data import register_tensor_from_anndata, synthetic_iid
from scvi.data._anndata import get_from_registry
from scvi.dataloaders import AnnDataLoader
from scvi.model.base import (
    BaseModelClass,
    PyroJitGuideWarmup,
    PyroSampleMixin,
    PyroSviTrainMixin,
)
from scvi.module.base import PyroBaseModuleClass
from scvi.train import PyroTrainingPlan, Trainer


class BayesianRegressionPyroModel(PyroModule):
    def __init__(self, in_features, out_features, n_obs):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.n_obs = n_obs

        self.register_buffer("zero", torch.tensor(0.0))
        self.register_buffer("one", torch.tensor(1.0))
        self.register_buffer("ten", torch.tensor(10.0))

        self.linear = PyroModule[nn.Linear](in_features, out_features)
        self.linear.weight = PyroSample(
            lambda prior: dist.Normal(self.zero, self.one)
            .expand([self.out_features, self.in_features])
            .to_event(2)
        )
        self.linear.bias = PyroSample(
            lambda prior: dist.Normal(self.zero, self.ten)
            .expand([self.out_features])
            .to_event(1)
        )

    def create_plates(self, x, y, ind_x):
        return pyro.plate("data", size=self.n_obs, dim=-2, subsample=ind_x)

    def list_obs_plate_vars(self):
        """Create a dictionary with the name of observation/minibatch plate,
        indexes of model args to provide to encoder,
        variable names that belong to the observation plate
        and the number of dimensions in non-plate axis of each variable"""

        return {
            "name": "obs_plate",
            "in": [0],  # index for expression data
            "sites": {},
        }

    @staticmethod
    def _get_fn_args_from_batch(tensor_dict):
        x = tensor_dict[_CONSTANTS.X_KEY]
        y = tensor_dict[_CONSTANTS.LABELS_KEY]
        ind_x = tensor_dict["ind_x"].long().squeeze()
        return (x, y, ind_x), {}

    @staticmethod
    def _get_fn_args_full_data(adata):
        x = get_from_registry(adata, _CONSTANTS.X_KEY)
        if issparse(x):
            x = np.asarray(x.toarray())
        x = torch.tensor(x.astype("float32"))
        ind_x = torch.tensor(get_from_registry(adata, "ind_x"))
        y = torch.tensor(get_from_registry(adata, _CONSTANTS.LABELS_KEY))
        return (x, y, ind_x), {}

    def forward(self, x, y, ind_x):

        obs_plate = self.create_plates(x, y, ind_x)

        sigma = pyro.sample("sigma", dist.Exponential(self.one))
        mean = self.linear(x).squeeze(-1)
        with obs_plate:
            pyro.sample("obs", dist.Normal(mean, sigma), obs=y)
        return mean


class BayesianRegressionModule(PyroBaseModuleClass):
    def __init__(self, **kwargs):

        super().__init__()
        self._model = BayesianRegressionPyroModel(**kwargs)
        self._guide = AutoNormal(
            self.model, init_loc_fn=init_to_mean, create_plates=self.model.create_plates
        )
        self._get_fn_args_from_batch = self._model._get_fn_args_from_batch

    @property
    def model(self):
        return self._model

    @property
    def guide(self):
        return self._guide


class BayesianRegressionModel(PyroSviTrainMixin, PyroSampleMixin, BaseModelClass):
    def __init__(
        self,
        adata: AnnData,
        batch_size=None,
    ):
        # add index for each cell (provided to pyro plate for correct minibatching)
        adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
        register_tensor_from_anndata(
            adata,
            registry_key="ind_x",
            adata_attr_name="obs",
            adata_key_name="_indices",
        )

        super().__init__(adata)

        self.batch_size = batch_size
        self.module = BayesianRegressionModule(
            in_features=adata.shape[1], out_features=1, n_obs=adata.n_obs
        )
        self._model_summary_string = "BayesianRegressionModel"
        self.init_params_ = self._get_init_params(locals())


def test_pyro_bayesian_regression(save_path):
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    # add index for each cell (provided to pyro plate for correct minibatching)
    adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
    register_tensor_from_anndata(
        adata,
        registry_key="ind_x",
        adata_attr_name="obs",
        adata_key_name="_indices",
    )
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegressionModule(
        in_features=adata.shape[1], out_features=1, n_obs=adata.n_obs
    )
    plan = PyroTrainingPlan(model, n_obs=len(train_dl.indices))
    trainer = Trainer(
        gpus=use_gpu,
        max_epochs=2,
    )
    trainer.fit(plan, train_dl)
    if use_gpu == 1:
        model.cuda()

    # test Predictive
    num_samples = 5
    predictive = model.create_predictive(num_samples=num_samples)
    for tensor_dict in train_dl:
        args, kwargs = model._get_fn_args_from_batch(tensor_dict)
        _ = {
            k: v.detach().cpu().numpy()
            for k, v in predictive(*args, **kwargs).items()
            if k != "obs"
        }
    # test save and load
    # cpu/gpu has minor difference
    model.cpu()
    quants = model.guide.quantiles([0.5])
    sigma_median = quants["sigma"][0].detach().cpu().numpy()
    linear_median = quants["linear.weight"][0].detach().cpu().numpy()

    model_save_path = os.path.join(save_path, "model_params.pt")
    torch.save(model.state_dict(), model_save_path)

    pyro.clear_param_store()
    new_model = BayesianRegressionModule(adata.shape[1], 1)
    # run model one step to get autoguide params
    try:
        new_model.load_state_dict(torch.load(model_save_path))
    except RuntimeError as err:
        if isinstance(new_model, PyroBaseModuleClass):
            plan = PyroTrainingPlan(new_model, n_obs=len(train_dl.indices))
            trainer = Trainer(
                gpus=use_gpu,
                max_steps=1,
            )
            trainer.fit(plan, train_dl)
            new_model.load_state_dict(torch.load(model_save_path))
        else:
            raise err

    quants = new_model.guide.quantiles([0.5])
    sigma_median_new = quants["sigma"][0].detach().cpu().numpy()
    linear_median_new = quants["linear.weight"][0].detach().cpu().numpy()

    np.testing.assert_array_equal(sigma_median_new, sigma_median)
    np.testing.assert_array_equal(linear_median_new, linear_median)


def test_pyro_bayesian_regression_jit():
    use_gpu = int(torch.cuda.is_available())
    adata = synthetic_iid()
    # add index for each cell (provided to pyro plate for correct minibatching)
    adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
    register_tensor_from_anndata(
        adata,
        registry_key="ind_x",
        adata_attr_name="obs",
        adata_key_name="_indices",
    )
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    pyro.clear_param_store()
    model = BayesianRegressionModule(
        in_features=adata.shape[1], out_features=1, n_obs=adata.n_obs
    )
    train_dl = AnnDataLoader(adata, shuffle=True, batch_size=128)
    plan = PyroTrainingPlan(
        model, loss_fn=pyro.infer.JitTrace_ELBO(), n_obs=len(train_dl.indices)
    )
    trainer = Trainer(
        gpus=use_gpu, max_epochs=2, callbacks=[PyroJitGuideWarmup(train_dl)]
    )
    trainer.fit(plan, train_dl)

    # 100 features, 1 for sigma, 1 for bias
    # assert list(model.guide.parameters())[0].shape[0] == 102

    if use_gpu == 1:
        model.cuda()

    # test Predictive
    num_samples = 5
    predictive = model.create_predictive(num_samples=num_samples)
    for tensor_dict in train_dl:
        args, kwargs = model._get_fn_args_from_batch(tensor_dict)
        _ = {
            k: v.detach().cpu().numpy()
            for k, v in predictive(*args, **kwargs).items()
            if k != "obs"
        }


def test_pyro_bayesian_train_sample_mixin():
    use_gpu = torch.cuda.is_available()
    adata = synthetic_iid()
    mod = BayesianRegressionModel(adata, batch_size=128)
    mod.train(max_epochs=2, lr=0.01, use_gpu=use_gpu)

    # 100 features, 1 for sigma, 1 for bias
    # assert list(mod.module.guide.parameters())[0].shape[0] == 102

    # test posterior sampling
    samples = mod.sample_posterior(num_samples=10, use_gpu=use_gpu, return_samples=True)

    assert len(samples["posterior_samples"]["sigma"]) == 10


def test_pyro_bayesian_train_sample_mixin_full_data():
    use_gpu = torch.cuda.is_available()
    adata = synthetic_iid()
    mod = BayesianRegressionModel(adata, batch_size=None)
    mod.train(max_epochs=2, lr=0.01, use_gpu=use_gpu)

    # 100 features, 1 for sigma, 1 for bias
    # assert list(mod.module.guide.parameters())[0].shape[0] == 102

    # test posterior sampling
    samples = mod.sample_posterior(num_samples=10, use_gpu=use_gpu, return_samples=True)

    assert len(samples["posterior_samples"]["sigma"]) == 10
