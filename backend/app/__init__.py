__all__ = ["app", "settings"]


def __getattr__(name):
    # 云端必须先安装访客配置，再加载业务接口和任务执行器。
    if name == 'app':
        from .main import app
        return app
    if name == 'settings':
        from .config import settings
        return settings
    raise AttributeError(name)
