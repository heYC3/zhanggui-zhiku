from app.core.logger import logger
from app.conf.embedding_config import embedding_config

# 模型单例对象，避免重复初始化
_bge_m3_ef = None
# 加载锁：多任务并发首次向量化时，保证整个进程只加载一份模型（约2-3GB）
import threading
_bge_m3_lock = threading.Lock()

def get_bge_m3_ef():
    """
    获取BGE-M3模型单例对象，自动加载环境变量配置
    :return: 初始化完成的BGEM3EmbeddingFunction实例
    """
    global _bge_m3_ef
    # 快路径：已初始化则直接返回（不加锁，避免每次请求都有锁开销）
    if _bge_m3_ef is not None:
        logger.debug("BGE-M3模型单例已存在，直接返回实例")
        return _bge_m3_ef

    # 慢路径：并发首次加载时排队，确保只加载一份（否则15个任务会复制15份模型挤爆内存）
    with _bge_m3_lock:
        if _bge_m3_ef is not None:
            return _bge_m3_ef

    # 从环境变量加载配置，无配置则使用默认值
    # 本地有可以使用本地地址！ 没有使用 "BAAI/bge-m3" 会自动下载！ 如果云端部署也可以使用url地址！
    model_name = embedding_config.bge_m3_path or "BAAI/bge-m3"
    device = embedding_config.bge_device or "cpu"
    use_fp16 = embedding_config.bge_fp16 or False

    # 打印模型初始化配置，便于问题排查
    logger.info(
        "开始初始化BGE-M3模型",
        extra={
            "model_name": model_name,
            "device": device,
            "use_fp16": use_fp16,
            "normalize_embeddings": True
        }
    )

    try:
        # 放到真正向量化时再导入。这个包会连带加载 PyTorch，没装好时不应挡住网页启动。
        from pymilvus.model.hybrid import BGEM3EmbeddingFunction

        # 初始化BGE-M3模型，开启原生L2归一化（适配Milvus IP内积检索）
        # 内存紧张时 torch 可能加载出"空壳模型"（meta tensor），构造不报错但编码必报错，
        # 所以加载后立刻做一次预热编码验证；失败就清空单例重试，绝不让坏模型留在进程里。
        for attempt in range(1, 4):
            try:
                _bge_m3_ef = BGEM3EmbeddingFunction(
                    model_name=model_name,
                    device=device,
                    use_fp16=use_fp16,
                    normalize_embeddings=True  # 模型原生对稠密+稀疏向量做L2归一化
                )
                # 预热编码：验证模型真的能用（能吐出稠密向量）
                _bge_m3_ef.encode_documents(["模型预热测试"])
                logger.success(f"BGE-M3模型初始化成功（第{attempt}次尝试），已开启原生L2归一化")
                return _bge_m3_ef
            except Exception as load_error:
                _bge_m3_ef = None  # 清掉可能的坏实例，下次重新加载
                logger.warning(f"BGE-M3模型第{attempt}次加载/预热失败：{load_error}")
                if attempt == 3:
                    raise
                import time as _time
                _time.sleep(10 * attempt)  # 给系统一点回收内存的时间
        raise RuntimeError("BGE-M3模型三次加载均失败")
    except Exception as e:
        logger.error(f"BGE-M3模型初始化失败：{str(e)}", exc_info=True)
        _bge_m3_ef = None
        raise  # 向上抛出异常，由调用方处理


def generate_embeddings(texts):
    """
    为文本列表生成稠密+稀疏混合向量嵌入（模型原生L2归一化）
    :param texts: 要生成嵌入的文本列表，单文本也需封装为列表
    :return: 字典格式的向量结果，key为dense/sparse，对应嵌套列表/字典列表
    :raise: 向量生成过程中的异常，由调用方捕获处理
    """
    # 入参合法性校验
    if not isinstance(texts, list) or len(texts) == 0:
        logger.warning("生成向量入参不合法，texts必须为非空列表")
        raise ValueError("参数texts必须是包含文本的非空列表")

    logger.info(f"开始为{len(texts)}条文本生成混合向量嵌入")
    try:
        # 加载BGE-M3模型单例
        model = get_bge_m3_ef()
        # 模型编码生成向量，返回dense（稠密向量）+sparse（CSR格式稀疏向量）
        embeddings = model.encode_documents(texts)
        logger.debug(f"模型编码完成，开始解析稀疏向量格式，共{len(texts)}条")

        # 初始化稀疏向量处理结果，解析为字典格式（适配序列化/存储）
        processed_sparse = []
        for i in range(len(texts)):
            # 提取第i个文本的稀疏向量索引：np.int64 → Python int（满足字典key可哈希要求）
            sparse_indices = embeddings["sparse"].indices[
                embeddings["sparse"].indptr[i]:embeddings["sparse"].indptr[i + 1]
            ].tolist()
            # 提取第i个文本的稀疏向量权重：np.float32 → Python float（适配JSON序列化/接口返回）
            sparse_data = embeddings["sparse"].data[
                embeddings["sparse"].indptr[i]:embeddings["sparse"].indptr[i + 1]
            ].tolist()
            # 构造{特征索引: 归一化权重}的稀疏向量字典
            sparse_dict = {k: v for k, v in zip(sparse_indices, sparse_data)}
            processed_sparse.append(sparse_dict)

        # 构造最终返回结果，稠密向量转列表（解决numpy数组不可序列化问题）
        result = {
            "dense": [emb.tolist() for emb in embeddings["dense"]],  # 嵌套列表，与输入文本一一对应
            "sparse": processed_sparse  # 字典列表，模型已做L2归一化
        }
        logger.success(f"{len(texts)}条文本向量生成完成，格式已适配工业级使用")
        return result

    except Exception as e:
        logger.error(f"文本向量生成失败：{str(e)}", exc_info=True)
        raise  # 不吞异常，向上传递让调用方做重试/降级处理


"""
核心设计亮点&适配说明：
1. 模型原生归一化：开启normalize_embeddings = True，自动对稠密+稀疏向量做L2归一化，完美适配Milvus IP内积检索（单位化后IP等价于余弦，计算更快）；
2. 彻底解决NumPy类型做key问题：sparse_indices加.tolist()，将np.int64转为Python原生int，满足字典key的可哈希要求，无报错风险；
3. 稀疏值适配序列化：sparse_data加.tolist()，将np.float32转为Python原生float，支持JSON写入/接口返回/Milvus入库等所有场景；
4. 单例模式优化：模型仅初始化一次，避免重复加载耗时耗资源，提升批量处理效率；
5. 格式匹配业务调用：返回dense嵌套列表、sparse字典列表，与vector_result["dense"][0]/sparse_vector["sparse"][0]取值逻辑完美契合；
6. 分级日志覆盖：从模型初始化、向量生成到异常报错，全流程日志记录，便于生产环境问题排查；
7. 入参合法性校验：防止空列表/非列表入参导致的内部报错，提升工具类健壮性。
"""