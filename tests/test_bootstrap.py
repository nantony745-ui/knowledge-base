from pathlib import Path

from dotenv import dotenv_values

from scripts import bootstrap_local


def test_generated_credentials_match_services_and_survive_second_run(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / '.env.example'
    (tmp_path / '.env.example').write_bytes(example.read_bytes())
    monkeypatch.setattr(bootstrap_local, 'ROOT', tmp_path)

    bootstrap_local.main()
    application = dotenv_values(tmp_path / '.env')
    tracing = dotenv_values(tmp_path / '.env.langfuse')
    keys = ('ADMIN_PASSWORD', 'JWT_SECRET', 'MYSQL_PASSWORD', 'MYSQL_ROOT_PASSWORD',
            'REDIS_PASSWORD', 'MINIO_ROOT_PASSWORD')
    assert all(len(application[key]) >= 32 and not application[key].startswith('replace-with-')
               for key in keys)
    assert len({application[key] for key in keys}) == len(keys)
    assert application['LANGFUSE_PUBLIC_KEY'] == tracing['LANGFUSE_INIT_PROJECT_PUBLIC_KEY']
    assert application['LANGFUSE_SECRET_KEY'] == tracing['LANGFUSE_INIT_PROJECT_SECRET_KEY']
    assert tracing['MINIO_ROOT_PASSWORD'] == tracing['LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY']
    assert application['CHAT_API_KEY'] == 'replace-with-chat-key'
    before = [(tmp_path / name).read_bytes() for name in ('.env', '.env.langfuse')]

    bootstrap_local.main()
    assert before == [(tmp_path / name).read_bytes() for name in ('.env', '.env.langfuse')]
    assert (tmp_path / '.env').stat().st_mode & 0o777 == 0o600


def test_existing_credentials_are_preserved(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('ADMIN_PASSWORD=existing-admin-value\n'
                                 'CHAT_API_KEY=existing-provider-value\n'
                                 'LANGFUSE_PUBLIC_KEY=existing-public-value\n'
                                 'LANGFUSE_SECRET_KEY=existing-secret-value\n')
    monkeypatch.setattr(bootstrap_local, 'ROOT', tmp_path)
    bootstrap_local.main()
    values = dotenv_values(tmp_path / '.env')
    assert values['ADMIN_PASSWORD'] == 'existing-admin-value'
    assert values['CHAT_API_KEY'] == 'existing-provider-value'
    assert values['LANGFUSE_PUBLIC_KEY'] == 'existing-public-value'
    assert values['LANGFUSE_SECRET_KEY'] == 'existing-secret-value'
