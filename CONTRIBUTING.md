# 参与掌柜智库

运行入口见[运行指南](docs/running.md)，结构见[架构与实现](docs/architecture.md)。Python版本为3.11。

## 验证改动

在`knowledge_base`中安装依赖并运行：

```bash
pip install -r requirements-listing.txt -r requirements-rag-tests.txt
python -m unittest discover -s test/listing -v
python -m unittest discover -s test/rag -v
python -m app.listing_agent.eval
```

测试不调用真实模型。已有本机`.env`时设置`PYTHON_DOTENV_DISABLED=1`，避免测试加载它。Listing基线表发生变化时，需说明数据、配置和口径变化；不要为了通过CI直接覆盖标准答案。

## 提交范围

提交可运行代码、合成测试和必要说明。不要提交密钥、填好的`.env`、厂商手册、私有题目、原始响应、运行数据库或模型权重。只更改`.env.example`中的占位说明。

公开评测方法见[评测说明](docs/evaluation.md)。已暴露题只作回归；新的首次验收需另外准备、核对和封存，不能将调参后的分数称为独立盲测。

PR描述说明问题、最终行为和验证结果。涉及流程或配置变化时同步更新对应文档。安全问题见[SECURITY.md](SECURITY.md)。
