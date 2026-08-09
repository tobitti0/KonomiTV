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
    r'(?:話|夜|幕|章|旅)?[^「\s@]*?(?:\s*「|\s|[)）])?([^」@]*)?'
)

# KonomiTV が ARIB 外字から変換する番組付属情報と、クライアントで番組付属情報として装飾している記号の一覧
## シリーズ判定用正規表現へ渡すタイトルからだけ除去し、DB に保存する元の番組タイトルには影響を与えない
## リスト外の [OVA] や [Season 2] など、作品名の一部である可能性がある角括弧表記は除去しない
ARIB_PROGRAM_DECORATION_MARKS = frozenset({
    '新', '終', '再', '交', '映', '手', '声', '多', '副', '字', '文', 'CC', 'OP', '二',
    'S', 'B', 'SS', '無', '無料', 'C', 'S1', 'S2', 'S3', 'MV', '双', 'デ', 'D', 'N',
    'W', 'P', 'H', 'HV', 'SD', '天', '解', '料', '前', '後', '初', '生', '販', '吹',
    'PPV', '演', '移', '他', '収', '・', '英', '韓', '中', '字/日', '字/日英', 'ほか',
    '3D', '2ndScr', '2K', '4K', '8K', '5.1', '7.1', '22.2', '60P', '120P', 'd',
    'HC', 'HDR', 'Hi-Res', 'Lossless', 'SHV', 'UHD', 'VOD', '配',
})

# 長い記号から並べることで、将来部分一致を含む表記が追加されても意図した候補を先に評価する
_ARIB_PROGRAM_DECORATION_PATTERN = re.compile(
    r'\((?:二|字|再)\)|\[(?:' +
    '|'.join(re.escape(mark) for mark in sorted(ARIB_PROGRAM_DECORATION_MARKS, key=len, reverse=True)) +
    r')\]'
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
    def normalizeTitle(cls, title: str) -> str:
        """
        シリーズ判定用に、タイトルから既知の ARIB 番組付属情報だけを除去する。
        表示・保存用の元タイトルを変更せず、正規表現によるシリーズ判定時にのみ利用する。

        Args:
            title (str): 正規化前の番組タイトル。

        Returns:
            str: ARIB 番組付属情報を除去した番組タイトル。
        """

        return _ARIB_PROGRAM_DECORATION_PATTERN.sub('', title)


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
        normalized_title = cls.normalizeTitle(title)
        # JavaScript の String.match() と同様に、アンカーがない正規表現はタイトル全体から一致箇所を探索する
        match = compiled_pattern.search(normalized_title)
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
