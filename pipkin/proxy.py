#!/usr/bin/env python3
"""
MIT License

Copyright (c) 2022 Aivar Annamaa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import copy
import io
import json
import logging
import shlex
import tarfile
from html.parser import HTMLParser
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import BaseServer
from textwrap import dedent
from typing import List, Dict, Optional, Tuple
from urllib.request import urlopen

from pipkin.common import UserError

MP_ORG_INDEX = "https://micropython.org/pi"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple"
SERVER_ENCODING = "utf-8"


try:
    from shlex import join as shlex_join
except ImportError:
    # before Python 3.8
    def shlex_join(split_command):
        """Return a shell-escaped string from *split_command*."""
        return " ".join(shlex.quote(arg) for arg in split_command)


import email.parser

logger = logging.getLogger(__name__)


class SimpleUrlsParser(HTMLParser):
    def error(self, message):
        pass

    def __init__(self):
        self._current_tag: str = ""
        self._current_attrs: List[Tuple[str, str]] = []
        self.file_urls: Dict[str, str] = {}
        super().__init__()

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        self._current_tag = tag
        self._current_attrs = attrs

    def handle_data(self, data: str) -> None:
        if self._current_tag == "a":
            for att, val in self._current_attrs:
                if att == "href":
                    self.file_urls[data] = val

    def handle_endtag(self, tag):
        pass


class BaseIndexDownloader:
    def __init__(self, index_url: str):
        self._index_url = index_url.rstrip("/")
        self._file_urls_cache: Dict[str, Dict[str, str]] = {}

    def get_file_urls(self, dist_name: str) -> Dict[str, str]:
        if dist_name not in self._file_urls_cache:
            self._file_urls_cache[dist_name] = self._download_file_urls(dist_name)

        return self._file_urls_cache[dist_name]

    def _download_file_urls(self, dist_name) -> Dict[str, str]:
        raise NotImplementedError()


class SimpleIndexDownloader(BaseIndexDownloader):
    def _download_file_urls(self, dist_name) -> Dict[str, str]:
        url = f"{self._index_url}/{dist_name}"
        logger.info("Downloading file urls from simple index %s", url)

        with urlopen(url) as fp:
            parser = SimpleUrlsParser()
            parser.feed(fp.read().decode("utf-8"))
            return parser.file_urls


class JsonIndexDownloader(BaseIndexDownloader):
    def _download_file_urls(self, dist_name) -> Dict[str, str]:
        metadata_url = f"{self._index_url}/{dist_name}/json"
        logger.info("Downloading file urls from json index at %s", metadata_url)

        result = {}
        with urlopen(metadata_url) as fp:
            data = json.load(fp)
            releases = data["releases"]
            for ver in releases:
                for file in releases[ver]:
                    file_url = file["url"]
                    if "filename" in file:
                        file_name = file["filename"]
                    else:
                        # micropython.org/pi doesn't have it
                        file_name = file_url.split("/")[-1]
                        # may be missing micropython prefix
                        if not file_name.startswith(dist_name):
                            # Let's hope version part doesn't contain dashes
                            _, suffix = file_name.split("-")
                            file_name = dist_name + "-" + suffix
                    result[file_name] = file_url

        return result


class PipkinProxy(HTTPServer):
    def __init__(
        self,
        prefer_mp_org: Optional[bool],
        index_url: Optional[str],
        extra_index_urls: List[str],
    ):
        self._downloaders = []
        self._downloaders_by_dist_name = {}
        if prefer_mp_org:
            self._downloaders.append(JsonIndexDownloader(MP_ORG_INDEX))
        self._downloaders.append(SimpleIndexDownloader(index_url or PYPI_SIMPLE_INDEX))
        for url in extra_index_urls:
            self._downloaders.append(SimpleIndexDownloader(url))
        super().__init__(("", 0), PipkinProxyHandler)

    def get_downloader_for_dist(self, dist_name: str) -> Optional[BaseIndexDownloader]:
        if dist_name not in self._downloaders_by_dist_name:
            for downloader in self._downloaders:
                file_urls = downloader.get_file_urls(dist_name)
                if file_urls:
                    self._downloaders_by_dist_name[dist_name] = downloader
                    break
            else:
                self._downloaders_by_dist_name[dist_name] = None

        return self._downloaders_by_dist_name[dist_name]

    def get_index_url(self) -> str:
        return f"http://{self.server_name}:{self.server_port}"


class PipkinProxyHandler(BaseHTTPRequestHandler):
    def __init__(self, request: bytes, client_address: Tuple[str, int], server: BaseServer):
        logger.debug("Creating new handler")
        assert isinstance(server, PipkinProxy)
        self.proxy: PipkinProxy = server
        super(PipkinProxyHandler, self).__init__(request, client_address, server)

    def do_GET(self) -> None:
        path = self.path.strip("/")
        logger.debug("do_GET for %s", path)
        if "/" in path:
            assert path.count("/") == 1
            self._serve_file(*path.split("/"))
        else:
            self._serve_distribution_page(path)

    def _serve_distribution_page(self, dist_name: str) -> None:
        logger.debug("Serving index page for %s", dist_name)
        downloader = self.proxy.get_downloader_for_dist(dist_name)
        if downloader is None:
            raise UserError(f"Distribution '{dist_name}' can't be found.")

        # TODO: check https://discuss.python.org/t/community-testing-of-packaging-tools-against-non-warehouse-indexes/13442

        file_urls = downloader.get_file_urls(dist_name)
        logger.debug("File urls: %r", file_urls)
        self.send_response(200)
        self.send_header("Content-type", f"text/html; charset={SERVER_ENCODING}")
        self.end_headers()
        self.wfile.write("<!DOCTYPE html><html><body>\n".encode(SERVER_ENCODING))
        for file_name in file_urls:
            self.wfile.write(
                f"<a href='/{dist_name}/{file_name}/'>{file_name}</a>\n".encode(SERVER_ENCODING)
            )
        self.wfile.write("</body></html>".encode(SERVER_ENCODING))

    def _serve_file(self, dist_name: str, file_name: str):
        logger.debug("Serving %s for %s", file_name, dist_name)

        original_bytes = self._download_file(dist_name, file_name)
        tweaked_bytes = self._tweak_file(dist_name, file_name, original_bytes)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        block_size = 4096
        for start_index in range(0, len(tweaked_bytes), block_size):
            block = tweaked_bytes[start_index : start_index + block_size]
            self.wfile.write(block)

    def _download_file(self, dist_name: str, file_name: str) -> bytes:
        downloader = self.proxy.get_downloader_for_dist(dist_name)
        assert downloader is not None

        urls = downloader.get_file_urls(dist_name)
        assert urls

        assert file_name in urls
        url = urls[file_name]
        logger.debug("Downloading file from %s", url)
        result = urlopen(url)
        logger.debug("Headers: %r", result.headers.items())
        return result.read()

    def _tweak_file(self, dist_name: str, file_name: str, original_bytes: bytes) -> bytes:
        if not file_name.lower().endswith(".tar.gz"):
            return original_bytes

        # In case of upip packages (tar.gz-s without setup.py) reverse following process:
        # https://github.com/micropython/micropython-lib/commit/3a6ab0b

        in_tar = tarfile.open(fileobj=io.BytesIO(original_bytes), mode="r:gz")
        out_buffer = io.BytesIO()
        out_tar = tarfile.open(fileobj=out_buffer, mode="w:gz")

        wrapper_dir = None
        py_modules = []
        packages = []
        metadata_bytes = None
        requirements = []
        egg_info_path = None

        for info in in_tar:
            logger.debug("Processing %r", info)
            with in_tar.extractfile(info) as f:
                content = f.read()

            if "/" in info.name:
                wrapper_dir, rel_name = info.name.split("/", maxsplit=1)
            else:
                assert info.isdir()
                wrapper_dir, rel_name = info.name, ""

            assert wrapper_dir.startswith(dist_name)

            rel_name = rel_name.strip("/")
            rel_segments = rel_name.split("/")

            out_info = copy.copy(info)

            # collect information about the original tar
            if rel_name == "setup.py":
                logger.debug("The archive contains setup.py. No tweaks needed")
                return original_bytes
            elif ".egg-info" in rel_name:
                if rel_name.endswith(".egg-info/PKG-INFO"):
                    egg_info_path = rel_name[: -len("/PKG-INFO")]
                    metadata_bytes = content
                elif rel_name.endswith(".egg-info/requires.txt"):
                    requirements = content.decode("utf-8").strip().splitlines()
            elif len(rel_segments) == 1:
                # toplevel item outside of egg-info
                if info.isfile() and rel_name.endswith(".py"):
                    # toplevel module
                    module_name = rel_name[: -len(".py")]
                    py_modules.append(module_name)
                else:
                    assert info.isdir()
                    # Assuming all toplevel directories represent packages.
                    packages.append(rel_name)
            else:
                # Assuming an item inside a subdirectory.
                # If it's a py, it will be included together with containing package,
                # otherwise it will be picked up by package_data wildcard expression.
                if rel_segments[0] not in packages:
                    # directories may not have their own entry
                    packages.append(rel_segments[0])

            # all existing files need to be added without changing
            out_tar.addfile(out_info, io.BytesIO(content))

        assert wrapper_dir
        assert metadata_bytes

        logger.debug("%s is optimized for upip. Re-constructing missing files", file_name)
        logger.debug("py_modules: %r", py_modules)
        logger.debug("packages: %r", packages)
        logger.debug("requirements: %r", requirements)
        metadata = self._parse_metadata(metadata_bytes)
        logger.debug("metadata: %r", metadata)
        setup_py = self._create_setup_py(metadata, py_modules, packages, requirements)
        logger.debug("setup.py: %s", setup_py)

        self._add_file_to_tar(wrapper_dir + "/setup.py", setup_py.encode("utf-8"), out_tar)
        self._add_file_to_tar(wrapper_dir + "/PKG-INFO", metadata_bytes, out_tar)
        self._add_file_to_tar(
            wrapper_dir + "/setup.cfg",
            b"""[egg_info]
tag_build = 
tag_date = 0
""",
            out_tar,
        )
        self._add_file_to_tar(
            wrapper_dir + "/" + egg_info_path + "/dependency_links.txt", b"\n", out_tar
        )
        self._add_file_to_tar(
            wrapper_dir + "/" + egg_info_path + "/top_level.txt",
            ("\n".join(packages + py_modules) + "\n").encode("utf-8"),
            out_tar,
        )

        # TODO: recreate SOURCES.txt and test with data files

        out_tar.close()

        out_bytes = out_buffer.getvalue()

        # with open("_temp.tar.gz", "wb") as fp:
        #    fp.write(out_bytes)

        return out_bytes

    def _add_file_to_tar(self, name: str, content: bytes, tar: tarfile.TarFile) -> None:
        stream = io.BytesIO(content)
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, stream)

    def _parse_metadata(self, metadata_bytes) -> Dict[str, str]:
        metadata = email.parser.Parser().parsestr(metadata_bytes.decode("utf-8"))
        return {
            key: metadata.get(key)
            for key in (
                "Metadata-Version",
                "Name",
                "Version",
                "Summary",
                "Home-page",
                "Author",
                "Author-email",
                "License",
            )
        }

    def _create_setup_py(
        self,
        metadata: Dict[str, str],
        py_modules: List[str],
        packages: List[str],
        requirements: List[str],
    ) -> str:

        src = dedent(
            f"""
            from setuptools import setup
            setup (
            """
        ).lstrip()

        for src_key, target_key in [
            ("Name", "name"),
            ("Version", "version"),
            ("Summary", "description"),
            ("Home-page", "url"),
            ("Author", "author"),
            ("Author-email", "author_email"),
            ("License", "license"),
        ]:
            if src_key in metadata:
                src += f"    {target_key}={metadata[src_key]!r},\n"

        if requirements:
            src += f"    install_required={requirements!r},\n"

        if py_modules:
            src += f"    py_modules={py_modules!r},\n"

        if packages:
            src += f"    packages={packages!r},\n"

        # include all other files as package data
        src += "    package_data={'*': ['*', '*/*', '*/*/*', '*/*/*/*', '*/*/*/*/*', '*/*/*/*/*/*', '*/*/*/*/*/*/*', '*/*/*/*/*/*/*/*']}\n"

        src += ")\n"
        return src
