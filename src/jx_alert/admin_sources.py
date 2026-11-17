"""Owner-only write-only credentials, exposed through the existing console guard."""
from jx_alert.lingxing.source_credentials import source_configuration, save_source, SourceCredentialsError


def get_data_sources(settings):
    return source_configuration(settings)


def save_data_source(settings, payload):
    if set(payload) != {"revision", "kind", "credentials", "expires_on"}:
        raise SourceCredentialsError("提交内容无效，请刷新数据源页面。", 400)
    return save_source(settings, **payload)
