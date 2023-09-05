import json
import logging
import os
from tkinter import N
from typing import Optional, Sequence, Union, List, Dict

import numpy as np
import pandas as pd
import torch
from pytorch_lightning.callbacks import EarlyStopping
from scvi.data import AnnDataManager
from scvi.dataloaders import DataSplitter
from torch.nn import functional as F
from scvi.data.fields import (
    LayerField,
    CategoricalObsField,
    NumericalObsField,
    ObsmField,
)

from anndata import AnnData
from scvi.model.base import BaseModelClass
from scvi.train import TrainRunner
from scvi.train._callbacks import SaveBestState
from scvi.utils import setup_anndata_dsp
from tqdm import tqdm

from ._module import CPAModule
from ._utils import CPA_REGISTRY_KEYS
from ._task import CPATrainingPlan
from ._data import AnnDataSplitter

logger = logging.getLogger(__name__)
logger.propagate = False


class CPA(BaseModelClass):
    """CPA model

    Parameters
    ----------
    adata : Anndata
        Registered Annotation Dataset

    n_latent: int
        Number of latent dimensions used for drug and Autoencoder

    recon_loss: str
        Either `gauss` or `NB`. Autoencoder loss function.

    doser_type: str
        Type of doser network. Either `sigm`, `logsigm` or `mlp`.

    split_key : str, optional
        Key used to split the data between train test and validation.
        This must correspond to a observation key for the adata, composed of values
        'train', 'test', and 'ood'. By default None.

    **hyper_params:
        CPA's hyper-parameters.


    Examples
    --------
    >>> import cpa
    >>> import scanpy as sc
    >>> adata = sc.read('dataset.h5ad')
    >>> adata = cpa.CPA.setup_anndata(adata,
                                      perturbation_keys={'perturbation': 'condition',  'dosage': 'dose_val'},
                                      categorical_covariate_keys=['cell_type'],
                                      control_key='control'
                                      )
    >>> hyperparams = {'autoencoder_depth': 3, 'autoencoder_width': 256}
    >>> model = cpa.CPA(adata,
                        n_latent=256,
                        loss_ae='gauss',
                        doser_type='logsigm',
                        split_key='split',
                        )
    """

    covars_encoder: dict = None
    pert_encoder: dict = None

    def __init__(
        self,
        adata: AnnData,
        split_key: str = None,
        train_split: Union[str, List[str]] = "train",
        valid_split: Union[str, List[str]] = "test",
        test_split: Union[str, List[str]] = "ood",
        **hyper_params,
    ):
        super().__init__(adata)

        self.split_key = split_key

        self.drugs = list(self.pert_encoder.keys())
        self.covars = {
            covar: list(self.covars_encoder[covar].keys())
            for covar in self.covars_encoder.keys()
        }

        self.module = CPAModule(
            n_genes=adata.n_vars,
            n_perts=len(self.pert_encoder),
            covars_encoder=self.covars_encoder,
            **hyper_params,
        ).float()

        train_indices, valid_indices, test_indices = None, None, None
        if split_key is not None:
            train_split = (
                train_split if isinstance(train_split, list) else [train_split]
            )
            valid_split = (
                valid_split if isinstance(valid_split, list) else [valid_split]
            )
            test_split = test_split if isinstance(test_split, list) else [test_split]

            train_indices = np.where(adata.obs.loc[:, split_key].isin(train_split))[0]
            valid_indices = np.where(adata.obs.loc[:, split_key].isin(valid_split))[0]
            test_indices = np.where(adata.obs.loc[:, split_key].isin(test_split))[0]

        self.train_indices = train_indices
        self.valid_indices = valid_indices
        self.test_indices = test_indices

        self._model_summary_string = f"Compositional Perturbation Autoencoder"

        self.init_params_ = self._get_init_params(locals())

        self.epoch_history = None

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        perturbation_key: str,
        control_group: str,
        dosage_key: Optional[str] = None,
        batch_key: Optional[str] = None,
        layer: Optional[str] = None,
        is_count_data: Optional[bool] = True,
        categorical_covariate_keys: Optional[List[str]] = None,
        deg_uns_key: Optional[str] = None,
        deg_uns_cat_key: Optional[str] = None,
        max_comb_len: int = 2,
        **kwargs,
    ):
        """
        Annotation Data setup function

        Parameters
        ----------
        adata: anndata.AnnData
            AnnData object
        perturbation_key: str
            Key in `adata.obs` containing perturbations
        control_group: str
            Control group name
        dosage_key: str, optional
            Key in `adata.obs` containing perturbation dosages, by default None. If None, all dosages are set to 1.0
        batch_key: str, optional
            Key in `adata.obs` containing batch information, by default None
        layer: str, optional
            Key in `adata.layers` containing gene expression data, by default None. If None, `adata.X` is used
        is_count_data: bool, optional
            Whether the data is count data, by default False
        categorical_covariate_keys: List[str], optional
            List of keys in `adata.obs` containing categorical covariates, by default None
        deg_uns_key: str, optional
            Key in `adata.uns` containing differentially expressed genes for each combination of covariates and perturbations, by default None
        deg_uns_cat_key: str, optional
            Key in `adata.obs` containing covariate combinations for each cell, by default None
        max_comb_len: int, optional
            Maximum number of perturbations in a combination, by default 2
        """
        CPA_REGISTRY_KEYS.PERTURBATION_KEY = perturbation_key
        CPA_REGISTRY_KEYS.PERTURBATION_DOSAGE_KEY = dosage_key
        CPA_REGISTRY_KEYS.CAT_COV_KEYS = categorical_covariate_keys
        CPA_REGISTRY_KEYS.MAX_COMB_LENGTH = max_comb_len
        CPA_REGISTRY_KEYS.BATCH_KEY = batch_key

        if dosage_key is None:
            print(f'Warning: dosage_key is not set. Setting it to "1.0" for all cells')

            dosage_key = 'CPA_dose_val'

            adata.obs[dosage_key] = adata.obs[perturbation_key].apply(lambda x: '+'.join(['1.0' for _ in x.split('+')])).values

        CPA_REGISTRY_KEYS.PERTURBATION_DOSAGE_KEY = dosage_key

        perturbations = adata.obs[perturbation_key].astype(str).values
        dosages = adata.obs[dosage_key].astype(str).values

        category_key = f"{cls.__name__}_cat"
        keys = categorical_covariate_keys + [perturbation_key]

        if batch_key is not None:
            keys = [batch_key] + keys

        adata.obs[category_key] = adata.obs[keys].apply(lambda x: "_".join(x), axis=1)
        CPA_REGISTRY_KEYS.CATEGORY_KEY = category_key

        if cls.pert_encoder is None:
            # get unique drugs
            perts_names_unique = set()
            for d in np.unique(perturbations):
                [perts_names_unique.add(i) for i in d.split("+") if i != control_group]
            perts_names_unique = ["<PAD>", control_group] + sorted(
                list(perts_names_unique)
            )
            CPA_REGISTRY_KEYS.PADDING_IDX = 0

            pert_encoder = {pert: i for i, pert in enumerate(perts_names_unique)}

        else:
            pert_encoder = cls.pert_encoder
            perts_names_unique = list(pert_encoder.keys())

        pert_map = {}
        for condition in tqdm(perturbations):
            perts_list = np.where(np.isin(perts_names_unique, condition.split("+")))[0]
            pert_map[condition] = list(perts_list) + [
                CPA_REGISTRY_KEYS.PADDING_IDX
                for _ in range(max_comb_len - len(perts_list))
            ]

        dose_map = {}
        for dosage_str in tqdm(dosages):
            dosages_list = [float(i) for i in dosage_str.split("+")]
            dose_map[dosage_str] = list(dosages_list) + [
                0.0 for _ in range(max_comb_len - len(dosages_list))
            ]

        data_perts = np.vstack(
            np.vectorize(lambda x: pert_map[x], otypes=[np.ndarray])(perturbations)
        ).astype(int)
        adata.obsm[CPA_REGISTRY_KEYS.PERTURBATIONS] = data_perts

        data_perts_dosages = np.vstack(
            np.vectorize(lambda x: dose_map[x], otypes=[np.ndarray])(dosages)
        ).astype(float)
        adata.obsm[CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES] = data_perts_dosages

        # setup control column
        control_key = f"{cls.__name__}_{control_group}"
        CPA_REGISTRY_KEYS.CONTROL_KEY = control_key
        adata.obs[control_key] = (adata.obs[perturbation_key] == control_group).astype(
            int
        )

        setup_method_args = cls._get_setup_method_args(**locals())
        anndata_fields = [
            LayerField(
                registry_key=CPA_REGISTRY_KEYS.X_KEY,
                layer=layer,
                is_count_data=is_count_data,
            ),
            ObsmField(
                CPA_REGISTRY_KEYS.PERTURBATIONS,
                CPA_REGISTRY_KEYS.PERTURBATIONS,
                is_count_data=True,
                correct_data_format=True,
            ),
            ObsmField(
                CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES,
                CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES,
                is_count_data=False,
                correct_data_format=True,
            ),
            CategoricalObsField(
                registry_key=CPA_REGISTRY_KEYS.PERTURBATION_KEY,
                attr_key=perturbation_key,
            ),
        ] + [
            CategoricalObsField(registry_key=covar, attr_key=covar)
            for covar in categorical_covariate_keys
        ]

        anndata_fields.append(
            NumericalObsField(registry_key=control_key, attr_key=control_key)
        )
        anndata_fields.append(
            CategoricalObsField(registry_key=category_key, attr_key=category_key)
        )

        if batch_key is not None:
            anndata_fields.append(
                CategoricalObsField(registry_key=batch_key, attr_key=batch_key)
            )

        if deg_uns_key:
            n_deg_r2 = kwargs.pop("n_deg_r2", 10)

            cov_cond_unique = np.unique(adata.obs[deg_uns_cat_key].astype(str).values)

            cov_cond_map = {}
            cov_cond_map_r2 = {}
            for cov_cond in tqdm(cov_cond_unique):
                if cov_cond in adata.uns[deg_uns_key].keys():
                    mask_hvg = adata.var_names.isin(
                        adata.uns[deg_uns_key][cov_cond]
                    ).astype(np.int)
                    mask_hvg_r2 = adata.var_names.isin(
                        adata.uns[deg_uns_key][cov_cond][:n_deg_r2]
                    ).astype(np.int)
                    cov_cond_map[cov_cond] = list(mask_hvg)
                    cov_cond_map_r2[cov_cond] = list(mask_hvg_r2)
                else:
                    no_mask = list(np.ones(shape=(adata.n_vars,)))
                    cov_cond_map[cov_cond] = no_mask
                    cov_cond_map_r2[cov_cond] = no_mask

            mask = np.vstack(
                np.vectorize(lambda x: cov_cond_map[x], otypes=[np.ndarray])(
                    adata.obs[deg_uns_cat_key].astype(str).values
                )
            )
            mask_r2 = np.vstack(
                np.vectorize(lambda x: cov_cond_map[x], otypes=[np.ndarray])(
                    adata.obs[deg_uns_cat_key].astype(str).values
                )
            )

            CPA_REGISTRY_KEYS.DEG_MASK = "deg_mask"
            CPA_REGISTRY_KEYS.DEG_MASK_R2 = "deg_mask_r2"
            adata.obsm[CPA_REGISTRY_KEYS.DEG_MASK] = np.array(mask)
            adata.obsm[CPA_REGISTRY_KEYS.DEG_MASK_R2] = np.array(mask_r2)

            anndata_fields.append(
                ObsmField(
                    CPA_REGISTRY_KEYS.DEG_MASK,
                    CPA_REGISTRY_KEYS.DEG_MASK,
                    is_count_data=True,
                    correct_data_format=True,
                )
            )
            anndata_fields.append(
                ObsmField(
                    CPA_REGISTRY_KEYS.DEG_MASK_R2,
                    CPA_REGISTRY_KEYS.DEG_MASK_R2,
                    is_count_data=True,
                    correct_data_format=True,
                )
            )

        adata_manager = AnnDataManager(
            fields=anndata_fields, setup_method_args=setup_method_args
        )
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

        keys = categorical_covariate_keys
        if batch_key is not None:
            keys.append(batch_key)

        covars_encoder = {}
        for covar in keys:
            covars_encoder[covar] = {
                c: i
                for i, c in enumerate(
                    adata_manager.registry["field_registries"][covar]["state_registry"][
                        "categorical_mapping"
                    ]
                )
            }

        if cls.covars_encoder is None:
            cls.covars_encoder = covars_encoder

        if cls.pert_encoder is None:
            cls.pert_encoder = pert_encoder

    def train(
        self,
        max_epochs: Optional[int] = None,
        use_gpu: Optional[Union[str, int, bool]] = None,
        train_size: float = 0.9,
        validation_size: Optional[float] = None,
        batch_size: int = 128,
        plan_kwargs: Optional[dict] = None,
        save_path: Optional[str] = None,
        check_val_every_n_epoch: int = 10,
        early_stopping_patience: int = 10,
        **trainer_kwargs,
    ):
        """
        Trains CPA on the given dataset

        Parameters
        ----------
        max_epochs: int
            Maximum number of epochs for training
        use_gpu: bool
            Whether to use GPU if available
        train_size: float
            Fraction of training data in the case of randomly splitting dataset to train/valdiation
                if `split_key` is not set in model's constructor
        validation_size: float
            Fraction of validation data in the case of randomly splitting dataset to train/valdiation
                if `split_key` is not set in model's constructor
        batch_size: int
            Size of mini-batches for training
        early_stopping: bool
            If `True`, EarlyStopping will be used during training on validation dataset
        plan_kwargs: dict
            `CPATrainingPlan` parameters
        save_path: str
            Path to save the model after the end of training
        check_val_every_n_epoch: int
            How often to check validation metrics
        early_stopping_patience: int
            Patience for early stopping
        **trainer_kwargs:
            Additional parameters for `cpa.CPATrainingPlan`
        """
        if max_epochs is None:
            n_cells = self.adata.n_obs
            max_epochs = np.min([round((20000 / n_cells) * 400), 400])
        plan_kwargs = plan_kwargs if isinstance(plan_kwargs, dict) else dict()

        manual_splitting = (
            (self.valid_indices is not None)
            and (self.train_indices is not None)
            and (self.test_indices is not None)
        )
        if manual_splitting:
            data_splitter = AnnDataSplitter(
                self.adata_manager,
                train_indices=self.train_indices,
                valid_indices=self.valid_indices,
                test_indices=self.test_indices,
                batch_size=batch_size,
                use_gpu=use_gpu,
            )
        else:
            data_splitter = DataSplitter(
                self.adata_manager,
                train_size=train_size,
                validation_size=validation_size,
                batch_size=batch_size,
                use_gpu=use_gpu,
            )

        perturbation_key = CPA_REGISTRY_KEYS.PERTURBATION_KEY
        pert_adv_encoder = {
            c: i
            for i, c in enumerate(
                self.adata_manager.registry["field_registries"][perturbation_key][
                    "state_registry"
                ]["categorical_mapping"]
            )
        }

        drug_weights = []
        n_adv_perts = len(self.adata.obs[perturbation_key].unique())
        for condition in tqdm(list(pert_adv_encoder.keys())):
            n_positive = len(self.adata[self.adata.obs[perturbation_key] == condition])
            drug_weights.append((self.adata.n_obs / n_positive) - 1.0)

        self.training_plan = CPATrainingPlan(
            self.module,
            self.covars_encoder,
            n_adv_perts=n_adv_perts,
            **plan_kwargs,
            drug_weights=drug_weights,
        )
        trainer_kwargs["early_stopping"] = False
        trainer_kwargs["check_val_every_n_epoch"] = check_val_every_n_epoch

        es_callback = EarlyStopping(
            monitor="cpa_metric",
            patience=early_stopping_patience,
            check_on_train_epoch_end=False,
            verbose=False,
            mode="max",
        )

        if "callbacks" in trainer_kwargs.keys() and isinstance(
            trainer_kwargs.get("callbacks"), list
        ):
            trainer_kwargs["callbacks"] += [es_callback]
        else:
            trainer_kwargs["callbacks"] = [es_callback]

        if save_path is None:
            save_path = "./"

        checkpoint = SaveBestState(
            monitor="cpa_metric", mode="max", period=1, verbose=True
        )
        trainer_kwargs["callbacks"].append(checkpoint)

        self.runner = TrainRunner(
            self,
            training_plan=self.training_plan,
            data_splitter=data_splitter,
            max_epochs=max_epochs,
            use_gpu=use_gpu,
            early_stopping_monitor="cpa_metric",
            early_stopping_mode="max",
            **trainer_kwargs,
        )
        self.runner()

        self.epoch_history = pd.DataFrame().from_dict(self.training_plan.epoch_history)
        if save_path is not False:
            self.save(save_path, overwrite=True)

    @torch.no_grad()
    def get_latent_representation(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        batch_size: Optional[int] = 32,
    ):
        """Returns All latent representations for the given dataset

        Parameters
        ----------
        adata : Optional[AnnData], optional
            [description], by default None
        indices : Optional[Sequence[int]], optional
            Optional indices, by default None
        batch_size : Optional[int], optional
            Batch size to use, by default None

        Returns
        -------
        latent_outputs : Dict[str, anndata.AnnData]
            Dictionary of latent representations containing:
                - 'latent_corrected': batch-corrected (if batch_key is set) latent representation
                - 'latent_basal': basal latent representation
                - 'latent_after': final latent representation which can be used for decoding.
        """

        if self.is_trained_ is False:
            raise RuntimeError("Please train the model first.")

        adata = self._validate_anndata(adata)
        if indices is None:
            indices = np.arange(adata.n_obs)
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size, shuffle=False
        )

        latent_basal = []
        latent = []
        latent_corrected = []
        for tensors in tqdm(scdl):
            tensors, _ = self.module.mixup_data(tensors, alpha=0.0)
            inference_inputs = self.module._get_inference_input(tensors)
            outputs = self.module.inference(**inference_inputs)
            latent_basal += [outputs["z_basal"].cpu().numpy()]
            latent += [outputs["z"].cpu().numpy()]
            latent_corrected += [outputs["z_corrected"].cpu().numpy()]

        latent_basal_adata = AnnData(
            X=np.concatenate(latent_basal, axis=0), obs=adata.obs.copy()
        )
        latent_basal_adata.obs_names = adata.obs_names

        latent_corrected_adata = AnnData(
            X=np.concatenate(latent_corrected, axis=0), obs=adata.obs.copy()
        )
        latent_corrected_adata.obs_names = adata.obs_names

        latent_adata = AnnData(X=np.concatenate(latent, axis=0), obs=adata.obs.copy())
        latent_adata.obs_names = adata.obs_names

        latent_outputs = {
            "latent_corrected": latent_corrected_adata,
            "latent_basal": latent_basal_adata,
            "latent_after": latent_adata,
        }

        return latent_outputs

    @torch.no_grad()
    def predict(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        batch_size: Optional[int] = 32,
        n_samples: int = 20,
        return_mean: bool = True,
    ):
        """Counterfactual-friendly gene expression prediction

        To produce counterfactuals, save `adata.X` to `adata.obsm['X_true']` 
        and set it to control cells gene expression.

        For the case of reconstruction, you can pass original adata 
        without any further modifications.

        Returns
        -------
        None (predictions are saved to `adata.obsm[f'CPA_pred']`)
        """
        assert self.module.recon_loss in ["gauss", "nb", "zinb"]
        self.module.eval()

        adata = self._validate_anndata(adata)
        if indices is None:
            indices = np.arange(adata.n_obs)
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size, shuffle=False
        )
        xs = []
        for tensors in tqdm(scdl):
            x_pred = (
                self.module.get_expression(tensors, n_samples=n_samples)
                .detach()
                .cpu()
                .numpy()
            )
            xs.append(x_pred)

        if n_samples > 1 and self.module.variational:
            # The -2 axis correspond to cells.
            x_pred = np.concatenate(xs, axis=1)
        else:
            x_pred = np.concatenate(xs, axis=0)

        if self.module.variational and n_samples > 1 and return_mean:
            x_pred = x_pred.mean(0)

        adata.obsm[f"{self.__class__.__name__}_pred"] = x_pred

    @torch.no_grad()
    def get_pert_embeddings(self, dosage=1.0, pert: Optional[str] = None):
        """Computes all/specific perturbation (e.g. drug) embeddings

        Parameters
        ----------
        dosage : float
            Dosage of interest, by default 1.0
        pert: str, optional
            Perturbation name if single perturbation embedding is desired
        
        Returns
        -------
        AnnData with perturbation embeddings in `.X` and perturbation names saved in `.obs['pert_name']`.

        """
        self.module.eval()
        if isinstance(dosage, float):
            if pert is None:
                n_drugs = len(self.pert_encoder)
                treatments = [torch.arange(n_drugs, device=self.device).long().unsqueeze(1)]
                for _ in range(CPA_REGISTRY_KEYS.MAX_COMB_LENGTH - 1):
                    treatments += [torch.zeros(n_drugs, device=self.device).long().unsqueeze(1) + CPA_REGISTRY_KEYS.PADDING_IDX]
                
                treatments = torch.cat(treatments, dim=1) # (n_drugs, max_comb_len)
                treatments_dosages = [torch.tensor([dosage for _ in range(n_drugs)], device=self.device).float().unsqueeze(1)] # (n_drugs, 1)
                for _ in range(CPA_REGISTRY_KEYS.MAX_COMB_LENGTH - 1):
                    treatments_dosages += [torch.zeros(n_drugs, device=self.device).float().unsqueeze(1) + CPA_REGISTRY_KEYS.PADDING_IDX]
                treatments_dosages = torch.cat(treatments_dosages, dim=1) # (n_drugs, max_comb_len)
            else:
                treatments = [self.pert_encoder[pert]] + [CPA_REGISTRY_KEYS.PADDING_IDX for _ in range(CPA_REGISTRY_KEYS.MAX_COMB_LENGTH - 1)]
                treatments = torch.LongTensor(treatments).to(self.device).unsqueeze(0)

                treatments_dosages = [dosage] + [CPA_REGISTRY_KEYS.PADDING_IDX for _ in range(CPA_REGISTRY_KEYS.MAX_COMB_LENGTH - 1)]
                treatments_dosages = torch.FloatTensor(treatments_dosages).to(self.device).unsqueeze(0)
        else:
            raise NotImplementedError

        embeds = self.module.pert_network(treatments, treatments_dosages).detach().cpu().numpy() # (1 or n_drugs, n_latent)
        pert_latent_adata = AnnData(X=embeds)
        pert_latent_adata.obs['pert_name'] = [pert] if pert is not None else self.pert_encoder.keys()

        return pert_latent_adata

    @torch.no_grad()
    def get_covar_embeddings(self, covariate: str, covariate_value: str = None):
        """Computes Covariate embeddings (e.g. cell_type, tissue, etc.)

        Parameters
        ----------
        covariate : str
            covariate to be computed
        covariate_value: str, Optional
            Covariate specific value for embedding computation

        Returns
        -------
        AnnData object with covariate embeddings in `.X` and covariate values in `.obs[covariate]`

        """
        # assert and print the error 
        assert covariate in self.covars_encoder.keys(), f"covariate {covariate} not found in learned covariates"
        self.module.eval()

        if covariate_value is None:
            covar_ids = torch.arange(
                len(self.covars_encoder[covariate]), device=self.device
            ).long().unsqueeze(1)
        else:
            covar_ids = torch.LongTensor(
                [self.covars_encoder[covariate][covariate_value]]
            ).to(self.device).long().unsqueeze(1)
        
        embeddings = self.module.covars_embeddings[covariate](covar_ids).detach().cpu().numpy() # (n_covars, n_latent)
        
        covar_latent_adata = AnnData(X=embeddings)
        covar_latent_adata.obs[covariate] = [covariate_value] if covariate_value is not None else self.covars_encoder[covariate].keys()

        return covar_latent_adata

    def save(
        self,
        dir_path: str,
        overwrite: bool = False,
        save_anndata: bool = False,
        **anndata_write_kwargs,
    ):
        """
        Saves the state of the model.

        Parameters
        ----------
        dir_path : `str`
            Path to a directory.
        overwrite : `bool`, optional (default: `False`)
            Whether to overwrite the model/data in `dir_path` if it already exists.
        save_anndata : `bool`, optional (default: `False`)
            Whether to save the anndata along with the model.
        **anndata_write_kwargs : keyword arguments
            Keyword arguments to pass to anndata's write function.
        """
        os.makedirs(dir_path, exist_ok=True)

        # save public dictionaries
        total_dict = {
            "pert_encoder": self.pert_encoder,
            "covars_encoder": self.covars_encoder,
        }

        json_dict = json.dumps(total_dict)
        with open(os.path.join(dir_path, "CPA_info.json"), "w") as f:
            f.write(json_dict)

        if isinstance(self.epoch_history, dict):
            self.epoch_history = pd.DataFrame().from_dict(
                self.training_plan.epoch_history
            )
            self.epoch_history.to_csv(
                os.path.join(dir_path, "history.csv"), index=False
            )
        elif isinstance(self.epoch_history, pd.DataFrame):
            self.epoch_history.to_csv(
                os.path.join(dir_path, "history.csv"), index=False
            )

        return super().save(
            dir_path=dir_path,
            overwrite=overwrite,
            save_anndata=save_anndata,
            **anndata_write_kwargs,
        )

    @classmethod
    def load(
        cls,
        dir_path: str,
        adata: Optional[AnnData] = None,
        use_gpu: Optional[Union[str, int, bool]] = None,
    ):
        """
        Loads the model from the specified directory.

        Parameters
        ----------
        dir_path : `str`
            Path to saved model.
        adata : `~anndata.AnnData`, optional (default: `None`)
            Annotated data matrix. Will call `cpa.CPA.setup_anndata` on the data after model restoration.
        use_gpu : `bool` or `str` or `int`, optional (default: `None`)
            Whether a GPU should be used. If `True`, will use GPU. 
        
        Returns
        -------
        :class:`~scvi.core.models.CPA`
            Restored model from the specified directory.
        """
        # load public dictionaries
        with open(os.path.join(dir_path, "CPA_info.json")) as f:
            total_dict = json.load(f)

            cls.pert_encoder = total_dict["pert_encoder"]
            cls.covars_encoder = total_dict["covars_encoder"]

        model = super().load(dir_path, adata, use_gpu)

        try:
            model.epoch_history = pd.read_csv(os.path.join(dir_path, "history.csv"))
        except:
            print("WARNING: The history was not found.")

        return model
