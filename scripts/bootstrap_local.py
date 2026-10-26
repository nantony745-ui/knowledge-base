import secrets
from dotenv import dotenv_values

from app.settings import ROOT


def write_values(path, values):
    path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')
    path.chmod(0o600)


def main() -> None:
    path = ROOT / '.env'
    values = dict(dotenv_values(path if path.exists() else ROOT / '.env.example'))
    for key in ('ADMIN_PASSWORD', 'JWT_SECRET', 'MYSQL_PASSWORD', 'MYSQL_ROOT_PASSWORD',
                'REDIS_PASSWORD', 'MINIO_ROOT_PASSWORD'):
        if not values.get(key) or values[key].startswith('replace-with-'):
            values[key] = secrets.token_hex(32)
    trace_path = ROOT / '.env.langfuse'
    if not trace_path.exists():
        postgres, minio = secrets.token_hex(32), secrets.token_hex(32)
        trace_values = {
            'NEXTAUTH_URL': 'http://127.0.0.1:3010',
            'NEXTAUTH_SECRET': secrets.token_hex(32), 'SALT': secrets.token_hex(32),
            'ENCRYPTION_KEY': secrets.token_hex(32), 'POSTGRES_PASSWORD': postgres,
            'DATABASE_URL': f'postgresql://postgres:{postgres}@postgres:5432/postgres',
            'CLICKHOUSE_PASSWORD': secrets.token_hex(32), 'MINIO_ROOT_PASSWORD': minio,
            'LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY': minio,
            'LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY': minio,
            'LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY': minio,
            'REDIS_AUTH': secrets.token_hex(32), 'TELEMETRY_ENABLED': 'false',
            'LANGFUSE_INIT_ORG_ID': 'knowledgebase-org', 'LANGFUSE_INIT_ORG_NAME': 'Knowledge Base',
            'LANGFUSE_INIT_PROJECT_ID': 'knowledge-base', 'LANGFUSE_INIT_PROJECT_NAME': 'Knowledge Base',
            'LANGFUSE_INIT_PROJECT_PUBLIC_KEY': 'pk-lf-' + secrets.token_hex(16),
            'LANGFUSE_INIT_PROJECT_SECRET_KEY': 'sk-lf-' + secrets.token_hex(16),
            'LANGFUSE_INIT_USER_EMAIL': 'admin@example.com',
            'LANGFUSE_INIT_USER_NAME': 'Knowledge Base Admin',
            'LANGFUSE_INIT_USER_PASSWORD': secrets.token_hex(32),
        }
        write_values(trace_path, trace_values)
    trace_values = dotenv_values(trace_path)
    for key, source in (('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_INIT_PROJECT_PUBLIC_KEY'),
                        ('LANGFUSE_SECRET_KEY', 'LANGFUSE_INIT_PROJECT_SECRET_KEY')):
        if not values.get(key) or values[key].startswith('replace-with-'):
            values[key] = trace_values[source]
    write_values(path, values)
    print('本地凭据已生成；已有有效值保留。请在 .env 中填写模型服务配置。')


if __name__ == '__main__':
    main()
