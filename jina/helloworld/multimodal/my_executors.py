import os
from typing import Dict, List

import numpy as np
import torch
import torchvision.models as models
from transformers import AutoModel, AutoTokenizer

from jina import Executor, DocumentArray, requests, Document
from jina.types.arrays.memmap import DocumentArrayMemmap


class Segmenter(Executor):
    @requests
    def segment(self, docs: DocumentArray, **kwargs):
        for doc in docs:
            text = doc.tags['caption']
            uri = f'{os.environ["HW_WORKDIR"]}/people-img/{doc.tags["image"]}'
            chunk_text = Document(text=text, mime_type='text/plain')
            chunk_uri = Document(uri=uri, mime_type='image/jpeg')
            doc.chunks = [chunk_text, chunk_uri]
            doc.uri = uri
            doc.convert_uri_to_datauri()


class TextEncoder(Executor):
    """Transformer executor class"""

    def __init__(
        self,
        pretrained_model_name_or_path: str = 'sentence-transformers/paraphrase-mpnet-base-v2',
        pooling_strategy: str = 'mean',
        layer_index: int = -1,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.pretrained_model_name_or_path = pretrained_model_name_or_path
        self.pooling_strategy = pooling_strategy
        self.layer_index = layer_index
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.pretrained_model_name_or_path
        )
        self.model = AutoModel.from_pretrained(
            self.pretrained_model_name_or_path, output_hidden_states=True
        )
        self.model.to(torch.device('cpu'))

    def _compute_embedding(self, hidden_states: 'torch.Tensor', input_tokens: Dict):
        fill_vals = {'cls': 0.0, 'mean': 0.0, 'max': -np.inf, 'min': np.inf}
        fill_val = torch.tensor(
            fill_vals[self.pooling_strategy], device=torch.device('cpu')
        )

        layer = hidden_states[self.layer_index]
        attn_mask = input_tokens['attention_mask'].unsqueeze(-1).expand_as(layer)
        layer = torch.where(attn_mask.bool(), layer, fill_val)

        embeddings = layer.sum(dim=1) / attn_mask.sum(dim=1)
        return embeddings.cpu().numpy()

    @requests
    def encode(self, docs: 'DocumentArray', **kwargs):
        with torch.no_grad():
            if not self.tokenizer.pad_token:
                self.tokenizer.add_special_tokens({'pad_token': '[PAD]'})
                self.model.resize_token_embeddings(len(self.tokenizer.vocab))

            input_tokens = self.tokenizer(
                docs.get_attributes('content'),
                padding='longest',
                truncation=True,
                return_tensors='pt',
            )
            input_tokens = {
                k: v.to(torch.device('cpu')) for k, v in input_tokens.items()
            }

            outputs = self.model(**input_tokens)
            hidden_states = outputs.hidden_states

            embeds = self._compute_embedding(hidden_states, input_tokens)
            docs.embeddings = embeds


class TextCrafter(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @requests()
    def filter(self, docs: DocumentArray, **kwargs):
        filtered_docs = DocumentArray(
            d for d in docs.traverse_flat(['c']) if d.mime_type == 'text/plain'
        )
        return filtered_docs


class ImageCrafter(Executor):
    @requests(on=['/index', '/search'])
    def craft(self, docs: DocumentArray, **kwargs):
        filtered_docs = DocumentArray(
            d for d in docs.traverse_flat(['c']) if d.mime_type == 'image/jpeg'
        )
        target_size = 224
        for doc in filtered_docs:
            doc.convert_uri_to_image_blob()
            doc.set_image_blob_size(target_size, target_size)
            doc.set_image_blob_channel_axis(-1, 0)
        return filtered_docs


class ImageEncoder(Executor):
    def __init__(
        self,
        model_name: str = 'mobilenet_v2',
        pool_strategy: str = 'mean',
        channel_axis: int = -1,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.channel_axis = channel_axis
        self.model_name = model_name
        self.pool_strategy = pool_strategy
        self.pool_fn = getattr(np, self.pool_strategy)
        model = getattr(models, self.model_name)(pretrained=True)
        self.model = model.features.eval()
        self.model.to(torch.device('cpu'))

    def _get_features(self, content):
        return self.model(content)

    def _get_pooling(self, feature_map: 'np.ndarray') -> 'np.ndarray':
        if feature_map.ndim == 2 or self.pool_strategy is None:
            return feature_map
        return self.pool_fn(feature_map, axis=(2, 3))

    @requests
    def encode(self, docs: DocumentArray, **kwargs):
        _input = torch.from_numpy(docs.blobs.astype('float32'))
        _features = self._get_features(_input).detach()
        _features = _features.numpy()
        _features = self._get_pooling(_features)
        docs.embeddings = _features


class DocVectorIndexer(Executor):
    def __init__(self, index_file_name: str, **kwargs):
        super().__init__(**kwargs)
        self._docs = DocumentArrayMemmap(self.workspace + f'/{index_file_name}')

    @requests(on='/index')
    def index(self, docs: 'DocumentArray', **kwargs):
        self._docs.extend(docs)

    @requests(on='/search')
    def search(self, docs: 'DocumentArray', parameters: Dict, **kwargs):
        docs.match(
            self._docs,
            metric='cosine',
            normalization=(1, 0),
            limit=int(parameters['top_k']),
        )


class KeyValueIndexer(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._docs = DocumentArrayMemmap(self.workspace + '/kv-idx')

    @requests(on='/index')
    def index(self, docs: DocumentArray, **kwargs):
        self._docs.extend(docs)

    @requests(on='/search')
    def query(self, docs: DocumentArray, **kwargs):
        for doc in docs:
            for match in doc.matches:
                extracted_doc = self._docs[match.parent_id]
                match.update(extracted_doc)


class WeightedRanker(Executor):
    @requests(on='/search')
    def rank(
        self, docs_matrix: List['DocumentArray'], parameters: Dict, **kwargs
    ) -> 'DocumentArray':
        """
        :param docs_matrix: list of :class:`DocumentArray` on multiple requests to
          get bubbled up matches.
        :param parameters: the parameters passed into the ranker, in this case stores :attr`top_k`
          to filter k results based on score.
        :param kwargs: not used (kept to maintain interface)
        """

        result_da = DocumentArray()  # length: 1 as every time there is only one query
        for d_mod1, d_mod2 in zip(*docs_matrix):

            final_matches = {}  # type: Dict[str, Document]

            for m in d_mod1.matches:
                m.scores['relevance'] = m.scores['cosine'].value * d_mod1.weight
                final_matches[m.parent_id] = Document(m, copy=True)

            for m in d_mod2.matches:
                if m.parent_id in final_matches:
                    final_matches[m.parent_id].scores['relevance'] = final_matches[
                        m.parent_id
                    ].scores['relevance'].value + (
                        m.scores['cosine'].value * d_mod2.weight
                    )
                else:
                    m.scores['relevance'] = m.scores['cosine'].value * d_mod2.weight
                    final_matches[m.parent_id] = Document(m, copy=True)

            da = DocumentArray(list(final_matches.values()))
            da.sort(key=lambda ma: ma.scores['relevance'].value, reverse=True)
            d = Document(matches=da[: int(parameters['top_k'])])
            result_da.append(d)
        return result_da
