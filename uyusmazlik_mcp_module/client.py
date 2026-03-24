# uyusmazlik_mcp_module/client.py

import httpx
from bs4 import BeautifulSoup
from typing import List, Optional
import logging
import re
import io
from markitdown import MarkItDown

from .models import (
    UyusmazlikSearchRequest,
    UyusmazlikApiDecisionEntry,
    UyusmazlikSearchResponse,
    UyusmazlikDocumentMarkdown,
)

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class UyusmazlikApiClient:
    BASE_URL = "https://kararlar.uyusmazlik.gov.tr"
    SEARCH_PATH = "/"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def __init__(self, request_timeout: float = 30.0):
        self.request_timeout = request_timeout

    async def _get_viewstate(self, client: httpx.AsyncClient) -> dict:
        """GET the root page to extract ASP.NET VIEWSTATE hidden fields."""
        r = await client.get(self.SEARCH_PATH, headers=self.HEADERS, timeout=self.request_timeout)
        r.raise_for_status()
        html_text = r.text
        vs  = re.search(r'id="__VIEWSTATE"\s+value="([^"]+)"', html_text)
        vsg = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]+)"', html_text)
        ev  = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]+)"', html_text)
        return {
            "__VIEWSTATE":          vs.group(1)  if vs  else "",
            "__VIEWSTATEGENERATOR": vsg.group(1) if vsg else "",
            "__EVENTVALIDATION":    ev.group(1)  if ev  else "",
        }

    async def search_decisions(self, params: UyusmazlikSearchRequest) -> UyusmazlikSearchResponse:
        """2-step search: GET VIEWSTATE → POST form."""
        query_parts = []
        if params.icerik:
            query_parts.append(params.icerik)
        if hasattr(params, 'hepsi') and params.hepsi:
            query_parts.append(params.hepsi)
        if hasattr(params, 'herhangi_birisi') and params.herhangi_birisi:
            query_parts.append(params.herhangi_birisi)
        search_text = ", ".join(query_parts) if query_parts else ""

        if hasattr(params, 'esas_sayisi') and params.esas_sayisi:
            scope = "EsasNo"
            search_text = params.esas_sayisi
        elif hasattr(params, 'karar_sayisi') and params.karar_sayisi:
            scope = "KararNo"
            search_text = params.karar_sayisi
        else:
            scope = "All"

        if not search_text:
            return UyusmazlikSearchResponse(decisions=[], total_records_found=0)

        logger.info(f"UyusmazlikApiClient: query='{search_text}', scope='{scope}'")

        async with httpx.AsyncClient(
            base_url=self.BASE_URL,
            verify=False,
            timeout=self.request_timeout,
            follow_redirects=True,
        ) as client:
            viewstate_fields = await self._get_viewstate(client)

            form_data = {
                **viewstate_fields,
                "__EVENTTARGET":   "",
                "__EVENTARGUMENT": "",
                "txtSearch":       search_text,
                "btnSearch":       "Ara",
                "rblSearchScope":  scope,
            }
            post_headers = {
                **self.HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.BASE_URL + "/",
            }
            response = await client.post(self.SEARCH_PATH, data=form_data, headers=post_headers)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        result_table = soup.find("table", {"id": "GridView1"})

        processed_decisions: List[UyusmazlikApiDecisionEntry] = []
        total_records: Optional[int] = None

        if result_table:
            rows = result_table.find_all("tr")
            data_rows = [r for r in rows if r.find("td")]
            total_records = len(data_rows)

            for row in data_rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                try:
                    esas_no   = cols[0].get_text(strip=True)
                    karar_no  = cols[1].get_text(strip=True)
                    karar_tar = cols[2].get_text(strip=True)

                    pdf_link_tag = cols[3].find("a") if len(cols) > 3 else None
                    pdf_rel = pdf_link_tag["href"] if pdf_link_tag and pdf_link_tag.has_attr("href") else None
                    pdf_url_str = (self.BASE_URL + "/" + pdf_rel) if pdf_rel else None

                    if not pdf_url_str:
                        continue

                    decision = UyusmazlikApiDecisionEntry(
                        esas_sayisi=esas_no,
                        karar_sayisi=karar_no,
                        bolum=None,
                        uyusmazlik_konusu=karar_tar,
                        karar_sonucu=None,
                        popover_content=f"Karar Tarihi: {karar_tar}",
                        document_url=pdf_url_str,
                        pdf_url=pdf_url_str,
                    )
                    processed_decisions.append(decision)
                except Exception as e:
                    logger.warning(f"UyusmazlikApiClient: Row parse error: {e}")

        logger.info(f"UyusmazlikApiClient: {len(processed_decisions)} decisions found.")
        return UyusmazlikSearchResponse(decisions=processed_decisions, total_records_found=total_records)

    def _convert_html_to_markdown_uyusmazlik(self, full_decision_html_content: str) -> Optional[str]:
        if not full_decision_html_content:
            return None
        import html as html_lib
        processed_html = html_lib.unescape(full_decision_html_content)
        try:
            html_bytes = processed_html.encode("utf-8")
            html_stream = io.BytesIO(html_bytes)
            md_converter = MarkItDown()
            result = md_converter.convert(html_stream)
            logger.info("UyusmazlikApiClient: Markdown conversion successful.")
            return result.text_content
        except Exception as e:
            logger.error(f"UyusmazlikApiClient: MarkItDown error: {e}")
            return None

    async def get_decision_document_as_markdown(self, document_url: str) -> UyusmazlikDocumentMarkdown:
        logger.info(f"UyusmazlikApiClient: Fetching document: {document_url}")
        try:
            async with httpx.AsyncClient(verify=False, timeout=self.request_timeout) as doc_client:
                r = await doc_client.get(
                    document_url,
                    headers={"Accept": "text/html,application/xhtml+xml,application/pdf,*/*"},
                    follow_redirects=True,
                )
            r.raise_for_status()
            if not r.text or not r.text.strip():
                return UyusmazlikDocumentMarkdown(source_url=document_url, markdown_content=None)
            markdown_content = self._convert_html_to_markdown_uyusmazlik(r.text)
            return UyusmazlikDocumentMarkdown(source_url=document_url, markdown_content=markdown_content)
        except Exception as e:
            logger.error(f"UyusmazlikApiClient: Error fetching {document_url}: {e}")
            raise

    async def close_client_session(self):
        logger.info("UyusmazlikApiClient: close_client_session called (no-op).")
