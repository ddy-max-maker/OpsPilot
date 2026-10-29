"""Embedding Provider 抽象（D4，PRD §6：可配置接口，版本锁定）。

- local_e5：本地 intfloat/multilingual-e5-small（384 维；需安装 embedding 依赖组
  `uv sync --group embedding`），按 e5 要求使用 query/passage 前缀；
- fake：确定性哈希向量（384 维）——CI 使用，不联网、不依赖私人 API Key。

版本快照（model_version）随 knowledge_versions / knowledge_embeddings 落库：
不同版本向量不静默混合检索（检索过滤 model_version），换模型 = 新增版本行。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import threading
from abc import ABC, abstractmethod
from functools import lru_cache

from app.core.config import Settings, get_settings

E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "


class EmbeddingError(RuntimeError):
    """Embedding Provider 构建或推理失败（不静默降级）。"""


class EmbeddingProvider(ABC):
    """Embedding 接口：模型名/版本/维度显式声明，随知识版本快照留档。

    D5 修复：本地模型推理不得阻塞 Worker 事件循环——统一经 aembed_* 在
    工作线程执行；ready=False 时调用方必须显式受控降级（不伪装检索成功）。
    """

    model_name: str
    model_version: str
    dimension: int
    chunking_version: str

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入知识分块（passage 语义；同步实现，勿在事件循环直接调用）。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """嵌入查询（query 语义；同步实现，勿在事件循环直接调用）。"""

    @property
    def ready(self) -> bool:
        """模型是否已就绪（fake 恒 True；本地模型加载完成前为 False）。"""
        return True

    @property
    def load_error(self) -> str | None:
        """预热/加载失败原因（None = 无失败）；供受控降级透出，不伪装成功。"""
        return None

    def warm_up(self) -> None:  # noqa: B027 — 默认空实现是接口约定（fake 无需预热）
        """预热（加载模型；允许阻塞，调用方应放在后台线程）。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_query, text)


class FakeEmbeddingProvider(EmbeddingProvider):
    """确定性哈希向量（CI）：同文本恒同向量，跨平台可复现，无外部依赖。"""

    def __init__(self, *, dimension: int = 384, chunking_version: str = "1") -> None:
        self.model_name = "fake-embedding"
        self.model_version = "fake-384-v1"
        self.dimension = dimension
        self.chunking_version = chunking_version

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(E5_PASSAGE_PREFIX, t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(E5_QUERY_PREFIX, text)

    def _embed_one(self, prefix: str, text: str) -> list[float]:
        digest = hashlib.sha256(f"{self.model_version}|{prefix}|{text}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        vec = [rng.gauss(0.0, 1.0) for _ in range(self.dimension)]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class LocalE5EmbeddingProvider(EmbeddingProvider):
    """本地 e5 多语言小模型（sentence-transformers；首次使用时加载/下载模型）。

    D5：模型加载移至 warm_up()（Worker 后台线程预热），推理经 aembed_* 在
    工作线程执行——事件循环/心跳/取消不再被阻塞；未就绪时 ready=False，
    调用方显式受控降级。

    D6：warm_up() 加进程级互斥锁——API 启动预热与首个发布请求可能并发调用，
    无锁会各自加载一份模型（双倍内存 + 双倍耗时）。
    """

    def __init__(self, *, model_name: str, dimension: int, chunking_version: str) -> None:
        self.model_name = model_name
        self.model_version = "e5-small-384-v1"
        self.dimension = dimension
        self.chunking_version = chunking_version
        self._model: object | None = None
        self._load_error: str | None = None
        self._load_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str | None:
        """预热失败原因（None = 无失败；供受控降级时透出，不伪装成功）。"""
        return self._load_error

    def warm_up(self) -> None:
        if self._model is not None:
            return
        # 双检锁：并发调用方共享同一次加载（等待者阻塞至模型就绪或加载失败）
        with self._load_lock:
            if self._model is not None:
                return
            if self._load_error is not None:
                raise EmbeddingError(self._load_error)
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover — 依赖组未安装
                self._load_error = (
                    "embedding provider 'local_e5' requires the 'embedding' dependency "
                    "group (uv sync --group embedding)"
                )
                raise EmbeddingError(self._load_error) from exc
            try:
                model = SentenceTransformer(self.model_name)
            except Exception as exc:  # noqa: BLE001 — 加载失败记录原因，调用方显式降级
                self._load_error = f"failed to load embedding model '{self.model_name}': {exc}"
                raise EmbeddingError(self._load_error) from exc
            model.max_seq_length = 512
            self._model = model
            self._load_error = None

    def _ensure_model(self) -> object:
        if self._model is None:
            self.warm_up()
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(  # type: ignore[attr-defined]
            [f"{E5_PASSAGE_PREFIX}{t}" for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure_model()
        vector = model.encode(  # type: ignore[attr-defined]
            [f"{E5_QUERY_PREFIX}{text}"],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        first: list[float] = vector[0].tolist()
        return first


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """按配置构建 Provider（未知 provider 显式报错，不静默降级）。"""
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(
            dimension=settings.embedding_dimension, chunking_version=settings.chunking_version
        )
    if settings.embedding_provider == "local_e5":
        return LocalE5EmbeddingProvider(
            model_name=settings.embedding_model_name,
            dimension=settings.embedding_dimension,
            chunking_version=settings.chunking_version,
        )
    raise EmbeddingError(f"unknown EMBEDDING_PROVIDER '{settings.embedding_provider}'")


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """进程级缓存 Provider（API 搜索与 Worker 工具复用；测试可显式传入实例）。"""
    return build_embedding_provider(get_settings())
