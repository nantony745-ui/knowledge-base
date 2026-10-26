import json
import time
from pathlib import Path

from app.adapters.common import AdapterError
from app.ingest.queue import redis_connection
from app.settings import Settings


def has_ready_documents() -> bool:
    from sqlalchemy import select
    from app.db.models import Document
    from app.db.session import session
    with session() as db:
        return db.scalar(select(Document.id).where(Document.parse_status == 'ready').limit(1)) is not None


class VectorStore:
    def __init__(self, settings: Settings):
        from pymilvus import MilvusClient
        self.settings = settings
        self.name = settings.MILVUS_COLLECTION
        try:
            self.client = MilvusClient(uri=settings.MILVUS_URI, timeout=settings.VECTOR_TIMEOUT)
        except Exception:
            raise AdapterError('vector_unavailable', True, 'Milvus连接失败，请检查服务状态', None) from None

    def ensure(self, dimension: int):
        from pymilvus import DataType
        s = self.settings
        identity = {'collection': self.name, 'model': s.EMBED_MODEL, 'base_url': s.EMBED_BASE_URL, 'dimension': dimension}
        path = s.DATA_DIR / 'index-space.json'
        with redis_connection().lock('personal_knora:collection-init', timeout=120, blocking_timeout=10):
            exists = self.client.has_collection(self.name, timeout=s.VECTOR_TIMEOUT)
            if exists:
                self.validate_schema(dimension)
                self.client.load_collection(self.name, timeout=s.VECTOR_TIMEOUT)
                if not path.exists() and self.client.query(self.name, limit=1, output_fields=['chunk_id'], consistency_level='Strong', timeout=s.VECTOR_TIMEOUT):
                    raise AdapterError('unknown_index_identity', False, '已有非空索引缺少模型身份记录，不能混用', None)
            if path.exists() and json.loads(path.read_text()) != identity:
                raise AdapterError('index_model_mismatch', False, '向量索引的模型身份与配置不一致', None)
            if not exists:
                if path.exists() or has_ready_documents():
                    raise AdapterError('collection_missing', False, '已有索引身份或就绪文档，但Milvus collection缺失，请恢复原索引', None)
                schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
                for name in ('chunk_id', 'kb_id', 'document_id'):
                    schema.add_field(name, DataType.VARCHAR, max_length=36, is_primary=name == 'chunk_id')
                schema.add_field('embedding', DataType.FLOAT_VECTOR, dim=dimension)
                indexes = self.client.prepare_index_params()
                indexes.add_index(field_name='embedding', index_type='HNSW', metric_type='COSINE', params={'M': 16, 'efConstruction': 200})
                self.client.create_collection(self.name, schema=schema, index_params=indexes, consistency_level='Strong', timeout=s.VECTOR_TIMEOUT)
            s.DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(identity), encoding='utf-8')
            temporary.replace(path)
            self.client.load_collection(self.name, timeout=s.VECTOR_TIMEOUT)

    def validate_schema(self, dimension):
        from pymilvus import DataType
        description = self.client.describe_collection(self.name, timeout=self.settings.VECTOR_TIMEOUT)
        fields = {field['name']: field for field in description['fields']}
        valid = set(fields) == {'chunk_id', 'kb_id', 'document_id', 'embedding'}
        valid &= not description.get('auto_id') and not description.get('enable_dynamic_field')
        for name in ('chunk_id', 'kb_id', 'document_id'):
            field = fields.get(name, {})
            valid &= field.get('type') == DataType.VARCHAR and int(field.get('params', {}).get('max_length', 0)) == 36
        valid &= fields.get('chunk_id', {}).get('is_primary', False)
        vector = fields.get('embedding', {})
        valid &= vector.get('type') == DataType.FLOAT_VECTOR and int(vector.get('params', {}).get('dim', 0)) == dimension
        if not valid:
            raise AdapterError('collection_schema_mismatch', False, 'Milvus字段、主键或维度与项目不一致', None)
        indexes = self.client.list_indexes(self.name, timeout=self.settings.VECTOR_TIMEOUT)
        descriptions = [self.client.describe_index(self.name, name, timeout=self.settings.VECTOR_TIMEOUT) for name in indexes]
        index = next((index for index in descriptions if index['field_name'] == 'embedding'), None)
        if index is None or index['metric_type'] != 'COSINE' or index['index_type'] != 'HNSW':
            raise AdapterError('collection_index_mismatch', False, 'Milvus索引必须使用HNSW和COSINE', None)
        params = index.get('params', index)
        if str(params.get('M')) != '16' or str(params.get('efConstruction')) != '200':
            raise AdapterError('collection_index_mismatch', False, 'Milvus HNSW参数必须为M=16、efConstruction=200', None)

    def batches(self, field: str, value: str, vectors: bool):
        from uuid import UUID
        value = str(UUID(value))
        iterator = self.client.query_iterator(self.name, filter=f'{field} == "{value}"',
                                              output_fields=['chunk_id', 'embedding'] if vectors else ['chunk_id'], batch_size=128, consistency_level='Strong', timeout=self.settings.VECTOR_TIMEOUT)
        try:
            while batch := iterator.next():
                yield batch
        finally:
            iterator.close()

    def records(self, field: str, value: str, vectors: bool):
        return [row for batch in self.batches(field, value, vectors) for row in batch]

    def verify(self, document_id: str, expected: set[str], dimension: int):
        deadline = time.monotonic() + self.settings.VERIFY_TIMEOUT
        while True:
            seen, valid = set(), True
            for batch in self.batches('document_id', document_id, bool(expected)):
                for row in batch:
                    seen.add(row['chunk_id'])
                    valid &= not expected or len(row['embedding']) == dimension
            if seen == expected and valid:
                return
            if time.monotonic() >= deadline:
                raise AdapterError('vector_visibility_timeout', True, '文档向量集合未完整可见', None)
            time.sleep(0.25)

    def remove(self, field: str, value: str):
        from uuid import UUID
        if not self.client.has_collection(self.name, timeout=self.settings.VECTOR_TIMEOUT):
            return
        value = str(UUID(value))
        self.client.delete(self.name, filter=f'{field} == "{value}"', timeout=self.settings.VECTOR_TIMEOUT)
        deadline = time.monotonic() + self.settings.VERIFY_TIMEOUT
        while self.records(field, value, False):
            if time.monotonic() >= deadline:
                raise AdapterError('vector_delete_timeout', True, '向量清理尚未确认，请重试', None)
            time.sleep(0.25)

    def write(self, rows: list[dict]):
        self.client.upsert(self.name, rows, timeout=self.settings.VECTOR_TIMEOUT)

    def search(self, kb_id: str, vector: list[float], limit: int):
        from uuid import UUID
        kb_id = str(UUID(kb_id))
        hits = self.client.search(self.name, data=[vector], filter=f'kb_id == "{kb_id}"', limit=limit,
                                  search_params={'metric_type': 'COSINE', 'params': {'ef': max(64, limit)}}, consistency_level='Strong', timeout=self.settings.VECTOR_TIMEOUT)[0]
        return [{'chunk_id': hit.get('chunk_id', hit.get('id')), 'score': hit['distance']} for hit in hits]

    def close(self):
        self.client.close()
