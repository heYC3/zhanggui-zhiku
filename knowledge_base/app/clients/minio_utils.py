# 导入Python内置模块
import json
import os
# 导入MinIO官方Python SDK核心类
from minio import Minio
# 项目内部配置与日志
from app.conf.minio_config import minio_config
from app.core.logger import logger

# 全局MinIO客户端对象，初始化后供全项目调用
minio_client = None
bucket_name = minio_config.bucket_name

def _init_minio_client():
    """
    启动时连接 MinIO 并确保图片桶存在。
    数据库还没启动时不能让整个网页服务起不来，失败只记日志，上传接口会跳过对象存储。
    """
    global minio_client
    if os.getenv('MANUAL_IMAGE_BACKEND', 'local').lower() == 'local':
        return None
    if not minio_config.endpoint or not bucket_name:
        logger.warning("MinIO 未配置 endpoint 或 bucket，图片将只保存在本地")
        return None
    try:
        client = Minio(
            endpoint=minio_config.endpoint,
            access_key=minio_config.access_key,
            secret_key=minio_config.secret_key,
            secure=minio_config.minio_secure,
        )
        if not client.bucket_exists(bucket_name):
            logger.info(f"MinIO存储桶[{bucket_name}]不存在，开始创建")
            client.make_bucket(bucket_name)
            logger.info(f"MinIO存储桶[{bucket_name}]创建成功")
        else:
            logger.info(f"MinIO存储桶[{bucket_name}]已存在，无需重复创建")

        # 允许浏览器通过链接直接打开手册里的图片
        bucket_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"]
            }]
        }
        client.set_bucket_policy(bucket_name, json.dumps(bucket_policy))
        logger.info(f"MinIO存储桶[{bucket_name}]已配置匿名只读，图片链接可直接打开")
        minio_client = client
        return client
    except Exception as e:
        logger.error(f"MinIO客户端初始化失败，图片上传会跳过，错误信息：{str(e)}", exc_info=True)
        minio_client = None
        return None


_init_minio_client()


def get_minio_client():
    """
    获取全局初始化的MinIO客户端实例
    :return: 已初始化的Minio对象 / None（初始化失败时）
    """
    return minio_client

if __name__ == "__main__":
    # 测试代码，请勿删除
    get_minio_client()
    pass
