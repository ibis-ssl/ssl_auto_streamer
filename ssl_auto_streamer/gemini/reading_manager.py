# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Reading Manager Agent - 発話候補を選別してリアルタイム性を維持する。

実況エージェントが生成したテキストをTTSで読み上げる際、
キューに溜まった発話候補の中から今読むべきものをLLMで選別する。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

_REST_BASE = "https://generativelanguage.googleapis.com/v1beta"

_SYSTEM_INSTRUCTION = """\
あなたはリアルタイムスポーツ実況の読み上げ管理者です。
実況者（AI）が生成したテキストの中から、今この瞬間に読む価値があるものを選別します。

## 選別の判断基準

1. **鮮度**: 既に状況が変化してしまった古い情報は捨てる
2. **優先度**: GOAL・ファール等の重要イベントに関する発話は最優先で残す
3. **重複回避**: 直近に読み上げた内容と同じ情報は捨てる
4. **流れ**: 途中から始まる断片的な文（文脈が失われている）は捨てる

## 制約（必須）

- **最大3件まで**選んでください。それ以上は捨ててください。
- 全部読む必要はありません。本当に今この瞬間に必要な情報だけを厳選してください。
- 候補が多い場合は積極的に捨ててください。少ない方が良い実況になります。

## 出力形式

読むべき候補の番号を、読む順に JSON 配列で返してください。
例: [2, 0]（2番を先に読み、次に0番を読む）
何も読まない場合は空配列: []

JSON 配列のみを返してください。説明文は不要です。\
"""

_SELECT_TEMPLATE = """\
現在の試合状況:
{game_context}

直近に読み上げた内容（重複回避の参考）:
{recently_spoken}

発話候補（優先度: 0=低 / 1=通常 / 2=高）:
{candidates}

上記の候補から読むべきものを選び、読む順に番号の JSON 配列で返してください。\
"""


@dataclass
class Utterance:
    """TTS に渡す発話単位。"""

    text: str
    priority: int  # 0=LOW, 1=NORMAL, 2=HIGH
    event_type: Optional[str] = None
    enqueued_at: float = field(default_factory=time.monotonic)
    id: int = 0  # UtteranceQueue が割り当てる連番


class ReadingManager:
    """
    読み上げ管理エージェント。

    発話候補リストと現在のゲーム状況を受け取り、
    今読むべき発話のインデックス（順序付き）を返す。
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-3.1-flash-lite-preview",
        timeout_seconds: float = 3.0,
    ):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._session: Optional[Any] = None

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not available; ReadingManager will use fallback only")
            return
        self._session = aiohttp.ClientSession()
        logger.info(f"ReadingManager started (model={self._model})")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def select(
        self,
        candidates: List[Utterance],
        game_context: Dict[str, Any],
        recently_spoken: List[str],
    ) -> List[int]:
        """
        候補リストから読むべき発話のインデックスを返す。

        Args:
            candidates: 発話候補リスト
            game_context: 現在の試合状況（スコア、経過時間等）
            recently_spoken: 直近に読み上げた内容（重複回避用）

        Returns:
            読む順に並んだインデックスのリスト。空なら何も読まない。
            API失敗時はフォールバックとして全インデックスを順番通り返す。
        """
        if not candidates:
            return []

        if not self._api_key or not AIOHTTP_AVAILABLE or not self._session:
            logger.debug("ReadingManager: fallback (no API key or session)")
            return list(range(len(candidates)))

        prompt = self._build_prompt(candidates, game_context, recently_spoken)

        try:
            result = await self._call_api(prompt)
            indices = self._parse_response(result, len(candidates))
            logger.info(
                f"ReadingManager: {len(candidates)} candidates → selected {indices}"
            )
            return indices
        except Exception as e:
            logger.warning(f"ReadingManager API error: {e}, using fallback")
            return list(range(len(candidates)))

    def _build_prompt(
        self,
        candidates: List[Utterance],
        game_context: Dict[str, Any],
        recently_spoken: List[str],
    ) -> str:
        candidates_text = "\n".join(
            f"[{i}] (優先度{utt.priority}, イベント:{utt.event_type or 'なし'}) {utt.text}"
            for i, utt in enumerate(candidates)
        )
        recently_text = (
            "\n".join(f"- {t}" for t in recently_spoken[-5:])
            if recently_spoken
            else "（なし）"
        )
        context_text = json.dumps(game_context, ensure_ascii=False)

        return _SELECT_TEMPLATE.format(
            game_context=context_text,
            recently_spoken=recently_text,
            candidates=candidates_text,
        )

    async def _call_api(self, prompt: str) -> str:
        url = (
            f"{_REST_BASE}/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": 0.0,  # 決定論的に選別
                "maxOutputTokens": 64,
            },
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with self._session.post(url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            body = await resp.json()

        candidates = body.get("candidates", [])
        if not candidates:
            raise ValueError("No candidates in ReadingManager response")

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p["text"] for p in parts if "text" in p]
        if not text_parts:
            raise ValueError("No text in ReadingManager response")

        return " ".join(text_parts).strip()

    def _parse_response(self, text: str, num_candidates: int) -> List[int]:
        """JSON配列をパースしてインデックスリストを返す。"""
        # JSON配列部分を抽出
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            raise ValueError(f"No JSON array in response: {text!r}")

        indices = json.loads(text[start : end + 1])
        if not isinstance(indices, list):
            raise ValueError(f"Response is not a list: {indices}")

        # 範囲チェックして有効なインデックスのみ返す
        valid = [i for i in indices if isinstance(i, int) and 0 <= i < num_candidates]
        return valid
