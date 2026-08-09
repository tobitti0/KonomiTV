import functools
import re
from dataclasses import dataclass
from typing import ClassVar


# TVDashboard で実運用されている番組タイトル解析用正規表現を初期値として採用する
## 第1キャプチャはシリーズ名、第2キャプチャは話数、第3キャプチャはサブタイトルとして扱う
DEFAULT_PROGRAM_TITLE_REGEX = (
    r'(?:(?:アニメA・|アニメギルド|tvアニメ|アニメ)\s*「?)?(.+?)\s*[」「（]?\s*'
    r'(?:最|第(?=(?![^#＃]*[#＃]\s*\d)[一二三四五六七八九十壱弐参拾〇零0-9,・~\-終]+(?:話|夜|幕|章|旅))|'
    r'[#＃]|症例|[Ee]pisode|Layer|[lL][vV]\.|karte\.|シフト|[Ee][Pp]|[cC]hapter|[（(]|'
    r'\s+(?=[一二三四五六七八九十壱弐参拾〇零0-9,・~\-終]+\s*$))'
    r'[\s:：]*?(?:第)?\s*([一二三四五六七八九十壱弐参拾〇零0-9,・~\-終]+)'
    r'(?:話|夜|幕|章|旅)?[^「\s@]*?(?:\s|「|[)）])?([^」@]*)?'
)


@dataclass(frozen=True, slots=True)
class ParsedProgramTitle:
    """正規表現から抽出した番組タイトルの構成要素を保持する。"""

    series_title: str
    episode_number: str | None
    subtitle: str | None


class ProgramTitleParser:
    """設定された正規表現を用いて番組タイトルをシリーズ情報へ分解する。"""

    # 同じ設定値で録画を連続解析する際に正規表現の再コンパイルを避ける
    ## Web のテスターでは未保存の正規表現も渡されるため、直近の複数パターンを保持できるサイズにする
    REGEX_CACHE_SIZE: ClassVar[int] = 16


    @classmethod
    @functools.lru_cache(maxsize=REGEX_CACHE_SIZE)
    def __compilePattern(cls, pattern: str) -> re.Pattern[str]:
        """
        番組タイトル解析用正規表現をコンパイルしてキャッシュする。

        Args:
            pattern (str): コンパイル対象の正規表現。

        Returns:
            re.Pattern[str]: 大文字・小文字を区別しないコンパイル済み正規表現。

        Raises:
            ValueError: 正規表現が空、構文が不正、またはキャプチャグループが存在しない場合。
        """

        # 空の正規表現はすべてのタイトルに一致してしまうため許可しない
        if pattern.strip() == '':
            raise ValueError('番組タイトルのシリーズ判定用正規表現を空にはできません。')

        # TVDashboard で大文字・小文字を区別せず利用している挙動に合わせる
        try:
            compiled_pattern = re.compile(pattern, flags=re.IGNORECASE)
        except re.error as ex:
            raise ValueError(f'番組タイトルのシリーズ判定用正規表現が不正です: {ex}') from ex

        # 第1キャプチャはシリーズ名として必須
        if compiled_pattern.groups < 1:
            raise ValueError('番組タイトルのシリーズ判定用正規表現には、シリーズ名を取得する第1キャプチャが必要です。')

        return compiled_pattern


    @classmethod
    def validatePattern(cls, pattern: str) -> str:
        """
        番組タイトル解析用正規表現を検証する。

        Args:
            pattern (str): 検証対象の正規表現。

        Returns:
            str: 検証済みの正規表現。

        Raises:
            ValueError: 正規表現が解析に利用できない場合。
        """

        cls.__compilePattern(pattern)
        return pattern


    @classmethod
    def parse(cls, title: str, pattern: str) -> ParsedProgramTitle | None:
        """
        番組タイトルをシリーズ名・話数・サブタイトルへ分解する。

        Args:
            title (str): 解析対象の番組タイトル。
            pattern (str): 番組タイトル解析用正規表現。

        Returns:
            ParsedProgramTitle | None: 解析結果。正規表現に一致しない場合は None。
        """

        compiled_pattern = cls.__compilePattern(pattern)
        # JavaScript の String.match() と同様に、アンカーがない正規表現はタイトル全体から一致箇所を探索する
        match = compiled_pattern.search(title)
        if match is None:
            return None

        # 第1キャプチャは必須だが、空白だけのシリーズ名は誤検出として扱う
        series_title = match.group(1).strip()
        if series_title == '':
            return None

        # 第2・第3キャプチャは設定された正規表現に存在する場合だけ取得する
        episode_number = match.group(2).strip() if compiled_pattern.groups >= 2 and match.group(2) is not None else None
        subtitle = match.group(3).strip() if compiled_pattern.groups >= 3 and match.group(3) is not None else None

        # 空文字列は API / DB 上で未取得を表す None に統一する
        if episode_number == '':
            episode_number = None
        if subtitle == '':
            subtitle = None

        return ParsedProgramTitle(
            series_title = series_title,
            episode_number = episode_number,
            subtitle = subtitle,
        )
