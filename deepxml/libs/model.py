import logging
import math
import os
import time
from scipy.sparse import lil_matrix
import _pickle as pickle
from .model_base import ModelBase
import torch.utils.data
from torch.utils.data import DataLoader
from .features import DenseFeatures
import numpy as np
import sys
import libs.utils as utils


class ModelFull(ModelBase):
    """
        Models with fully connected output layer
    """

    def __init__(self, params, net, criterion, optimizer):
        super().__init__(params, net, criterion, optimizer)
        self.feature_indices = params.feature_indices

    def _pp_with_shortlist(self, shorty, data_dir, dataset, model_dir,
                           model_fname, tr_feat_fname='trn_X_Xf.txt',
                           tr_label_fname='trn_X_Y.txt',
                           normalize_features=True, normalize_labels=False,
                           data={'X': None, 'Y': None}, keep_invalid=False,
                           feature_indices=None, label_indices=None,
                           batch_size=128, num_workers=4, aux_mapping=None):
        """Post-process with shortlist.
        Train an ANN without touching the classifier
        """
        dataset = self._create_dataset(
            os.path.join(data_dir, dataset),
            fname_features=tr_feat_fname,
            fname_labels=tr_label_fname,
            data=data,
            mode='predict',
            keep_invalid=keep_invalid,
            normalize_features=normalize_features,
            normalize_labels=normalize_labels,
            feature_indices=feature_indices,
            label_indices=label_indices,
            aux_mapping=aux_mapping)

        self.logger.info("Post-processing with shortlist!")
        shorty.reset()
        start_time = time.time()
        doc_embeddings = self.get_embeddings(
            None, None, dataset.features.data, batch_size, num_workers)



        shortlist_utils.update(
            data_loader, self, self.embedding_dims, shorty, flag=1)
        end_time = time.time()
        fname = os.path.join(model_dir, model_fname+'_ANN.pkl')
        shorty.save(fname)
        self.logger.info(
            "Time in post-process: {} sec., Model size: {} MB".format(
                end_time-start_time, os.path.getsize(fname)/math.pow(2, 20)))
        return shorty


class ModelShortlist(ModelBase):
    """
        Models with label shortlist
    """

    def __init__(self, params, net, criterion, optimizer, shorty):
        super().__init__(params, net, criterion, optimizer)
        self.shorty = shorty
        self.feature_indices = params.feature_indices
        self.label_indices = params.label_indices
        self.retrain_hnsw_after = params.retrain_hnsw_after
        self.update_shortlist = params.update_shortlist

    def _combine_scores_one(self, out_logits, batch_sim, beta):
        return beta*torch.sigmoid(out_logits) \
            + (1-beta)*torch.sigmoid(batch_sim)

    def _compute_loss_one(self, _pred, _true, _mask):
        # Compute loss for one classifier
        _true = _true.to(_pred.get_device())
        if _mask is not None:
            _mask = _mask.to(_true.get_device())
        return self.criterion(_pred, _true, _mask).to(self.devices[-1])

    def _compute_loss(self, out_ans, batch_data, weightage=1.0):
        # Support loss for parallel classifier as well
        if self.num_clf_partitions > 1:
            out = []
            temp = zip(out_ans, batch_data['Y'], batch_data['Y_mask'])
            for _, _out in enumerate(temp):
                out.append(self._compute_loss_one(*_out))
            return torch.stack(out).mean()
        else:
            return self._compute_loss_one(
                out_ans, batch_data['Y'], batch_data['Y_mask'])

    def _combine_scores(self, out_logits, batch_sim, beta):
        if isinstance(out_logits, list):  # For distributed classifier
            out = []
            for _, (_logits, _sim) in enumerate(zip(out_logits, batch_sim)):
                out.append(self._combine_scores_one(
                    _logits.data.cpu(), _sim.data, beta))
            return out
        else:
            return self._combine_scores_one(
                out_logits.data.cpu(), batch_sim.data, beta)

    def _strip_padding_label(self, mat, num_labels):
        stripped_vals = {}
        for key, val in mat.items():
            stripped_vals[key] = val[:, :num_labels].tocsr()
            del val
        return stripped_vals

    def _fit_shorty(self, features, labels, doc_embeddings=None,
                    use_coarse=True, feature_type='sparse'):
        if doc_embeddings is None:
            doc_embeddings = self.get_embeddings(
                data=features,
                feature_type=feature_type,
                return_coarse=use_coarse)
        self.shorty.fit(doc_embeddings, labels)

    def _update_shortlist(self, dataset, use_coarse=True, mode='train',
                          flag=True):
        if flag:
            if isinstance(dataset.features, DenseFeatures) and use_coarse:
                self.logger.info("Using pre-trained embeddings for shortlist.")
                doc_embeddings = dataset.features.data
            else:
                doc_embeddings = self.get_embeddings(
                    data=dataset.features.data,
                    return_coarse=use_coarse)
            if mode == 'train':
                self.shorty.reset()
                self._fit_shorty(
                    features=None,
                    labels=dataset.labels.data,
                    doc_embeddings=doc_embeddings)
            dataset.update_shortlist(
                *self._predict_shorty(doc_embeddings))

    def _predict_shorty(self, doc_embeddings):
        return self.shorty.query(doc_embeddings)

    def _update_predicted_shortlist(self, count, batch_size, predicted_labels,
                                    batch_out, batch_data, beta, top_k=50):
        _score = self._combine_scores(batch_out, batch_data['Y_sim'], beta)
        # IF rev mapping exist; case of distributed classifier
        if 'Y_map' in batch_data:
            batch_shortlist = batch_data['Y_map'].numpy()
            # Send this as merged?
            _knn_score = torch.cat(batch_data['Y_sim'], 1)
            _clf_score = torch.cat(batch_out, 1).data
            _score = torch.cat(_score, 1)
        else:
            batch_shortlist = batch_data['Y_s'].numpy()
            _knn_score = batch_data['Y_sim']
            _clf_score = batch_out.data
        utils.update_predicted_shortlist(
            count, batch_size, _clf_score, predicted_labels['clf'],
            batch_shortlist, top_k)
        utils.update_predicted_shortlist(
            count, batch_size, _knn_score, predicted_labels['knn'],
            batch_shortlist, top_k)
        utils.update_predicted_shortlist(
            count, batch_size, _score, predicted_labels['combined'],
            batch_shortlist, top_k)

    def _validate(self, data_loader, beta=0.2):
        self.net.eval()
        torch.set_grad_enabled(False)
        num_labels = data_loader.dataset.num_labels
        offset = 1 if self.label_padding_index is not None else 0
        _num_labels = data_loader.dataset.num_labels + offset
        num_instances = data_loader.dataset.num_instances
        num_batches = num_instances//data_loader.batch_size
        mean_loss = 0
        predicted_labels = {}
        predicted_labels['combined'] = lil_matrix((num_instances, _num_labels))
        predicted_labels['knn'] = lil_matrix((num_instances, _num_labels))
        predicted_labels['clf'] = lil_matrix((num_instances, _num_labels))
        count = 0
        for batch_idx, batch_data in enumerate(data_loader):
            batch_size = batch_data['batch_size']
            out_ans = self.net.forward(batch_data)
            loss = self._compute_loss(out_ans, batch_data)/batch_size
            mean_loss += loss.item()*batch_size
            self._update_predicted_shortlist(
                count, batch_size, predicted_labels, out_ans, batch_data, beta)
            count += batch_size
            if batch_idx % self.progress_step == 0:
                self.logger.info(
                    "Validation progress: [{}/{}]".format(
                        batch_idx, num_batches))
        return self._strip_padding_label(predicted_labels, num_labels), \
            mean_loss / num_instances

    def _fit(self, train_loader, validation_loader, model_dir, result_dir,
             init_epoch, num_epochs, validate_after, beta, use_coarse):
        for epoch in range(init_epoch, init_epoch+num_epochs):
            cond = self.dlr_step != -1 and epoch % self.dlr_step == 0
            if epoch != 0 and cond:
                self._adjust_parameters()
            batch_train_start_time = time.time()
            if epoch % self.retrain_hnsw_after == 0:
                self.logger.info(
                    "Updating shortlist at epoch: {}".format(epoch))
                shorty_start_t = time.time()
                self._update_shortlist(
                    dataset=train_loader.dataset,
                    use_coarse=use_coarse,
                    mode='train',
                    flag=self.shorty is not None)

                if validation_loader is not None:
                    self._update_shortlist(
                        dataset=validation_loader.dataset,
                        use_coarse=use_coarse,
                        mode='predict',
                        flag=self.shorty is not None)
                shorty_end_t = time.time()
                self.logger.info("ANN train time: {0:.2f} sec".format(
                    shorty_end_t - shorty_start_t))
                self.tracking.shortlist_time = self.tracking.shortlist_time \
                    + shorty_end_t - shorty_start_t
                batch_train_start_time = time.time()
            tr_avg_loss = self._step(train_loader, batch_div=True)
            self.tracking.mean_train_loss.append(tr_avg_loss)
            batch_train_end_time = time.time()
            self.tracking.train_time = self.tracking.train_time + \
                batch_train_end_time - batch_train_start_time

            self.logger.info(
                "Epoch: {:d}, loss: {:.6f}, time: {:.2f} sec".format(
                    epoch, tr_avg_loss,
                    batch_train_end_time - batch_train_start_time))
            if validation_loader is not None and epoch % validate_after == 0:
                val_start_t = time.time()
                predicted_labels, val_avg_loss = self._validate(
                    validation_loader, beta)
                val_end_t = time.time()
                _acc = self.evaluate(
                    validation_loader.dataset.labels.data, predicted_labels)
                self.tracking.validation_time = self.tracking.validation_time \
                    + val_end_t - val_start_t
                self.tracking.mean_val_loss.append(val_avg_loss)
                self.tracking.val_precision.append(_acc['combined'][0])
                self.tracking.val_ndcg.append(_acc['combined'][1])
                self.logger.info("Model saved after epoch: {}".format(epoch))
                self.save_checkpoint(model_dir, epoch+1)
                self.tracking.last_saved_epoch = epoch
                self.logger.info(
                    "P@1 (combined): {:.2f}, P@1 (knn): {:.2f}, P@1"
                    " (clf): {:.2f}, loss: {:.6f}, time: {:.2f} sec".format(
                        _acc['combined'][0][0]*100, _acc['knn'][0][0]*100,
                        _acc['clf'][0][0]*100, val_avg_loss,
                        val_end_t-val_start_t))
            self.tracking.last_epoch += 1

        self.save_checkpoint(model_dir, epoch+1)
        self.tracking.save(os.path.join(result_dir, 'training_statistics.pkl'))
        self.logger.info(
            "Training time: {:.2f} sec, Validation time: {:.2f} sec, "
            "Shortlist time: {:.2f} sec, Model size: {:.2f} MB".format(
                self.tracking.train_time,
                self.tracking.validation_time,
                self.tracking.shortlist_time,
                self.model_size))

    def fit(self, data_dir, model_dir, result_dir, dataset, learning_rate,
            num_epochs, data=None, tr_feat_fname='trn_X_Xf.txt',
            tr_label_fname='trn_X_Y.txt', val_feat_fname='tst_X_Xf.txt',
            val_label_fname='tst_X_Y.txt', batch_size=128, num_workers=4,
            shuffle=False, init_epoch=0, keep_invalid=False,
            feature_indices=None, label_indices=None, normalize_features=True,
            normalize_labels=False, validate=False, beta=0.2, use_coarse=True,
            shortlist_method='static', validate_after=5, aux_mapping=None,
            feature_type='sparse', tr_pretrained_shortlist=None,
            val_pretrained_shortlist=None):
        pretrained_shortlist = tr_pretrained_shortlist is not None
        self.logger.info("Loading training data.")
        train_dataset = self._create_dataset(
            os.path.join(data_dir, dataset),
            fname_features=tr_feat_fname,
            fname_labels=tr_label_fname,
            data=data,
            mode='train',
            keep_invalid=keep_invalid,
            normalize_features=normalize_features,
            size_shortlist=self.shortlist_size,
            normalize_labels=normalize_labels,
            feature_indices=feature_indices,
            shortlist_method=shortlist_method,
            feature_type=feature_type,
            label_indices=label_indices,
            aux_mapping=aux_mapping,
            pretrained_shortlist=tr_pretrained_shortlist,
            _type='shortlist')
        train_loader = self._create_data_loader(
            train_dataset,
            feature_type=train_dataset.feature_type,
            classifier_type='shortlist',
            batch_size=batch_size,
            num_workers=num_workers,
            shuffle=shuffle)
        # No need to update embeddings
        if self.freeze_embeddings and feature_type != 'dense':
            self.logger.info(
                "Computing and reusing coarse document embeddings "
                "to save computations.")
            data = {'X': None, 'Y': None}
            data['X'] = self.get_embeddings(
                data_dir=None,
                fname=None,
                data=train_dataset.features.data,
                return_coarse=True)
            data['Y'] = train_dataset.labels.data
            train_dataset = self._create_dataset(
                os.path.join(data_dir, dataset),
                data=data,
                fname_features=None,
                mode='train',
                normalize_features=False,  # do not normalize dense features
                shortlist_method=shortlist_method,
                size_shortlist=self.shortlist_size,
                feature_type='dense',
                pretrained_shortlist=tr_pretrained_shortlist,
                keep_invalid=True,   # Invalid labels already removed
                _type='shortlist')
            train_loader = self._create_data_loader(
                train_dataset,
                feature_type='dense',
                classifier_type='shortlist',
                batch_size=batch_size,
                num_workers=num_workers,
                shuffle=shuffle)
        self.logger.info("Loading validation data.")
        validation_loader = None
        if validate:
            validation_dataset = self._create_dataset(
                os.path.join(data_dir, dataset),
                fname_features=val_feat_fname,
                fname_labels=val_label_fname,
                data={'X': None, 'Y': None},
                mode='predict',
                size_shortlist=self.shortlist_size,
                keep_invalid=keep_invalid,
                normalize_features=normalize_features,
                normalize_labels=normalize_labels,
                feature_type=feature_type,
                feature_indices=feature_indices,
                pretrained_shortlist=val_pretrained_shortlist,
                label_indices=label_indices,
                aux_mapping=aux_mapping,
                _type='shortlist')
            validation_loader = self._create_data_loader(
                validation_dataset,
                feature_type=validation_dataset.feature_type,
                classifier_type='shortlist',
                batch_size=batch_size,
                num_workers=num_workers)
        self._fit(train_loader, validation_loader, model_dir,
                  result_dir, init_epoch, num_epochs,
                  validate_after, beta, use_coarse)

    def _predict(self, data_loader, top_k, use_coarse, **kwargs):
        beta = kwargs['beta'] if 'beta' in kwargs else 0.5
        self.logger.info("Loading test data.")
        self.net.eval()
        num_labels = data_loader.dataset.num_labels
        offset = 1 if self.label_padding_index is not None else 0
        _num_labels = data_loader.dataset.num_labels + offset
        torch.set_grad_enabled(False)
        self.logger.info("Fetching shortlist.")
        self._update_shortlist(
            dataset=data_loader.dataset,
            use_coarse=use_coarse,
            mode='predict',
            flag=self.shorty is not None)
        num_instances = data_loader.dataset.num_instances
        num_batches = num_instances//data_loader.batch_size

        predicted_labels = {}
        predicted_labels['combined'] = lil_matrix((num_instances, _num_labels))
        predicted_labels['knn'] = lil_matrix((num_instances, _num_labels))
        predicted_labels['clf'] = lil_matrix((num_instances, _num_labels))

        count = 0
        for batch_idx, batch_data in enumerate(data_loader):
            batch_size = batch_data['batch_size']
            out_ans = self.net.forward(batch_data)
            self._update_predicted_shortlist(
                count, batch_size, predicted_labels,
                out_ans, batch_data, beta, top_k)
            count += batch_size
            if batch_idx % self.progress_step == 0:
                self.logger.info(
                    "Prediction progress: [{}/{}]".format(
                        batch_idx, num_batches))
            del batch_data
        return self._strip_padding_label(predicted_labels, num_labels)

    def predict(self, data_dir, dataset, data=None,
                ts_feat_fname='tst_X_Xf.txt', ts_label_fname='tst_X_Y.txt',
                batch_size=256, num_workers=6, keep_invalid=False,
                feature_indices=None, label_indices=None, top_k=50,
                normalize_features=True, normalize_labels=False,
                aux_mapping=None, feature_type='sparse',
                pretrained_shortlist=None, use_coarse=True, **kwargs):
        dataset = self._create_dataset(
            os.path.join(data_dir, dataset),
            fname_features=ts_feat_fname,
            fname_labels=ts_label_fname,
            data=data,
            mode='predict',
            feature_type=feature_type,
            size_shortlist=self.shortlist_size,
            _type='shortlist',
            pretrained_shortlist=pretrained_shortlist,
            keep_invalid=keep_invalid,
            normalize_features=normalize_features,
            normalize_labels=normalize_labels,
            feature_indices=feature_indices,
            label_indices=label_indices,
            aux_mapping=aux_mapping)
        data_loader = self._create_data_loader(
            feature_type=feature_type,
            classifier_type='shortlist',
            dataset=dataset,
            batch_size=batch_size,
            num_workers=num_workers)
        time_begin = time.time()
        predicted_labels = self._predict(
            data_loader, top_k, use_coarse, **kwargs)
        time_end = time.time()
        prediction_time = time_end - time_begin
        acc = self.evaluate(dataset.labels.data, predicted_labels)
        _res = self._format_acc(acc)
        self.logger.info(
            "Prediction time (total): {:.2f} sec., "
            "Prediction time (per sample): {:.2f} msec., "
            "P@k(%): {:s}".format(
                prediction_time,
                prediction_time*1000/data_loader.dataset.num_instances, _res))
        return predicted_labels

    def save_checkpoint(self, model_dir, epoch):
        # Avoid purge call from base class
        super().save_checkpoint(model_dir, epoch, False)
        if self.shorty is not None:
            self.tracking.saved_checkpoints[-1]['ANN'] \
                = 'checkpoint_ANN_{}.pkl'.format(epoch)
            self.shorty.save(os.path.join(
                model_dir, self.tracking.saved_checkpoints[-1]['ANN']))
        self.purge(model_dir)

    def load_checkpoint(self, model_dir, fname, epoch):
        super().load_checkpoint(model_dir, fname, epoch)
        if self.shorty is not None:
            fname = os.path.join(model_dir, 'checkpoint_ANN_{}'.format(epoch))
            self.shorty.load(fname)

    def save(self, model_dir, fname):
        super().save(model_dir, fname)
        if self.shorty is not None:
            self.shorty.save(os.path.join(model_dir, fname+'_ANN'))

    def load(self, model_dir, fname):
        super().load(model_dir, fname)
        if self.shorty is not None:
            self.shorty.load(os.path.join(model_dir, fname+'_ANN'))

    def purge(self, model_dir):
        if self.shorty is not None:
            if len(self.tracking.saved_checkpoints) \
                    > self.tracking.checkpoint_history:
                fname = self.tracking.saved_checkpoints[0]['ANN']
                self.shorty.purge(fname)  # let the class handle the deletion
        super().purge(model_dir)

    @property
    def model_size(self):
        s = self.net.model_size
        if self.shorty is not None:
            return s + self.shorty.model_size
        return s


class ModelNS(ModelBase):
    """
        Models with negative sampling
    """

    def __init__(self, params, net, criterion, optimizer, shorty):
        super().__init__(params, net, criterion, optimizer)
        self.shorty = shorty
        self.feature_indices = params.feature_indices
        self.label_indices = params.label_indices

    def _strip_padding_label(self, mat, num_labels):
        stripped_vals = {}
        for key, val in mat.items():
            stripped_vals[key] = val[:, :num_labels].tocsr()
            del val
        return stripped_vals

    def fit(self, data_dir, model_dir, result_dir, dataset,
            learning_rate, num_epochs, data=None,
            tr_feat_fname='trn_X_Xf.txt', tr_label_fname='trn_X_Y.txt',
            val_feat_fname='tst_X_Xf.txt', val_label_fname='tst_X_Y.txt',
            batch_size=128, num_workers=4, shuffle=False, init_epoch=0,
            keep_invalid=False, feature_indices=None, label_indices=None,
            normalize_features=True, normalize_labels=False, validate=False,
            validate_after=5, *args, **kwargs):
        self.logger.info("Loading training data.")

        train_dataset = self._create_dataset(
            os.path.join(data_dir, dataset),
            fname_features=tr_feat_fname,
            fname_labels=tr_label_fname,
            data=data,
            mode='train',
            keep_invalid=keep_invalid,
            normalize_features=normalize_features,
            normalize_labels=normalize_labels,
            feature_indices=feature_indices,
            label_indices=label_indices,
            shortlist_method='dynamic',
            shorty=self.shorty)
        train_loader = self._create_data_loader(
            train_dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            shuffle=shuffle)
        # No need to update embeddings
        if self.freeze_embeddings:
            self.logger.info(
                "Computing and reusing document embeddings"
                "to save computations.")
            data = {'X': None, 'Y': None}
            data['X'] = self._document_embeddings(train_loader)
            data['Y'] = train_dataset.labels.data
            #  Invalid labels already removed
            train_dataset = self._create_dataset(
                os.path.join(data_dir, dataset),
                data=data,
                fname_features=None,
                mode='train',
                shortlist_method='dynamic',
                feature_type='dense',
                normalize_features=False,
                keep_invalid=True)
            train_loader = self._create_data_loader(
                train_dataset,
                batch_size=batch_size,
                num_workers=num_workers,
                shuffle=shuffle)

        self.logger.info("Loading validation data.")
        validation_loader = None
        if validate:
            validation_dataset = self._create_dataset(
                os.path.join(data_dir, dataset),
                fname_features=val_feat_fname,
                fname_labels=val_label_fname,
                data={'X': None, 'Y': None},
                mode='predict',
                keep_invalid=keep_invalid,
                normalize_features=normalize_features,
                normalize_labels=normalize_labels,
                feature_indices=feature_indices,
                label_indices=label_indices,
                size_shortlist=-1)  # No shortlist during prediction
            validation_loader = self._create_data_loader(
                validation_dataset,
                batch_size=batch_size,
                num_workers=num_workers)
        self._fit(train_loader, validation_loader,
                  model_dir, result_dir, init_epoch, 
                  num_epochs, validate_after)


class ModelReRanker(ModelShortlist):
    """
        Models with label shortlist
    """

    def __init__(self, params, net, criterion, optimizer, shorty):
        super().__init__(params, net, criterion, optimizer, shorty)

    def _combine_scores_one(self, out_logits, batch_sim, beta):
        return out_logits + batch_sim
