"""平台探针本地验证；不连接 PythonAnywhere 或 Shopify。"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/pythonanywhere" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PythonAnywhereProbeTest(unittest.TestCase):
    def test_production_proxy_is_opt_in_and_rejects_untrusted_hosts(self):
        from app.listing_agent.shopify_adapter import _shopify_proxies, ShopError
        with patch.dict(os.environ, {"LISTING_SHOPIFY_TRANSPORT": "direct"}):
            self.assertEqual(_shopify_proxies(), {})
        with patch.dict(os.environ, {"LISTING_SHOPIFY_TRANSPORT": "pythonanywhere"}):
            with patch("urllib.request.getproxies", return_value={"https": "http://proxy.server:3128"}):
                self.assertEqual(_shopify_proxies(), {"https": "http://proxy.server:3128"})
            for proxy in ("http://other.example:3128", "http://name:password@proxy.server:3128", "http://proxy.server:8080"):
                with patch("urllib.request.getproxies", return_value={"https": proxy}):
                    with self.assertRaises(ShopError):
                        _shopify_proxies()

    def test_pythonanywhere_storage_is_explicit_and_stays_in_account_home(self):
        from app.listing_agent.deployment import validate_public_config
        home = Path("/home/zhanggui").resolve()
        env = {
            "LISTING_DEPLOYMENT": "public", "LISTING_PLATFORM": "pythonanywhere", "USER": "zhanggui",
            "LISTING_SHOP_BACKEND": "shopify", "SHOPIFY_STORE_DOMAIN": "zhanggui-test.myshopify.com",
            "SHOPIFY_ADMIN_TOKEN": "fake", "LISTING_DRAFT_MODE": "offline", "LISTING_CHECKPOINT": "sqlite",
            "PYTHON_DOTENV_DISABLED": "1", "LISTING_ACCOUNTS": "owner:" + "x" * 25 + ":editor:org",
            "LISTING_RUNTIME_DIR": str(home / ".local/share/zhanggui/listing-runtime"),
            "LISTING_PUBLIC_ORIGIN": "https://zhanggui.pythonanywhere.com",
            "LISTING_SHOPIFY_TRANSPORT": "pythonanywhere",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(Path, "home", return_value=home), patch("urllib.request.getproxies", return_value={"https": "http://proxy.server:3128"}):
            validate_public_config()
            with patch.dict(os.environ, {"LISTING_RUNTIME_DIR": str(Path(tempfile.gettempdir()).resolve() / "runtime")}):
                with self.assertRaises(RuntimeError):
                    validate_public_config()
            with patch.dict(os.environ, {"LISTING_PUBLIC_ORIGIN": "https://other.pythonanywhere.com"}):
                with self.assertRaises(RuntimeError):
                    validate_public_config()

    def test_asgi_boot_marker_survives_restart_and_exposes_no_write_route(self):
        module = load("probe_app")
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with TestClient(module.create_app(runtime)) as client:
                first = client.get("/health").json()
                self.assertTrue(first["ok"])
                self.assertEqual(client.post("/health").status_code, 405)
                self.assertEqual(client.get("/api/listing/tasks").status_code, 404)
            with TestClient(module.create_app(runtime)) as client:
                second = client.get("/health").json()
            self.assertEqual(first["marker"], second["marker"])
            self.assertEqual(second["boot_count"], first["boot_count"] + 1)

    def test_shopify_probe_is_fixed_read_query_and_does_not_return_token(self):
        module = load("shopify_readonly")
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "private.env"
            env.write_text("SHOPIFY_STORE_DOMAIN=" + module.STORE + "\nSHOPIFY_ADMIN_TOKEN=fake-private-token\n", encoding="utf-8")
            opener = MagicMock()
            response = opener.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"data": {"shop": {"myshopifyDomain": module.STORE},
                                                            "products": {"nodes": [{"status": "DRAFT"}]}}}).encode()
            with patch.object(module.urllib.request, "build_opener", return_value=opener):
                proof = module.run(env)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://" + module.STORE + "/admin/api/2026-10/graphql.json")
            query = json.loads(request.data)["query"]
            self.assertTrue(query.startswith("query "))
            self.assertNotIn("mutation", query)
            self.assertNotIn("fake-private-token", json.dumps(proof))
            self.assertEqual(proof["sample_statuses"], ["DRAFT"])

    def test_shopify_probe_refuses_other_store_without_network(self):
        module = load("shopify_readonly")
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "private.env"
            env.write_text("SHOPIFY_STORE_DOMAIN=other.myshopify.com\nSHOPIFY_ADMIN_TOKEN=fake\n", encoding="utf-8")
            with patch.object(module.urllib.request, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    module.run(env)
                opener.assert_not_called()
