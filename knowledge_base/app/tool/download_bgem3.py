from modelscope.hub.snapshot_download import snapshot_download

# 填本机缓存目录。默认下到当前目录的 models，不要写死盘符。
model_dir = snapshot_download('BAAI/bge-m3', cache_dir='./models')
print(f"模型已下载到: {model_dir}")