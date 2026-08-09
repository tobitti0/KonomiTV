import unittest

from app.metadata.ProgramTitleParser import (
    ARIB_PROGRAM_DECORATION_MARKS,
    DEFAULT_PROGRAM_TITLE_REGEX,
    DEFAULT_PROGRAM_TITLE_REGEX_RULES,
    ProgramTitleParser,
    ProgramTitleRegexRule,
)


REFERENCE_TITLES = [
    'アリス・ギア・アイギス Expansion #01「さらば成子坂製作所!」',
    'アニメ 虚構推理 Season2 #17 六花ふたたび',
    '転生したらスライムだった件 第2期 第2部 第37話「訪れる者たち」',
    '文豪ストレイドッグス #49 文豪ハウンドドッグス',
    'アニメ アリス・ギア・アイギス Expansion 第1話',
    'アニメ うる星やつら<HDリマスター版>(第2話)',
    'くまクマ熊ベアーぱーんち! #01',
    'かぐや様は告らせたい-ファーストキッスは終わらない-第2夜',
    '山田くんとLv999の恋をする #01「これだからっ!ゲームする男なんて!!」',
    '痛いのは嫌なので防御力に極振りしたいと思います。2 第10話',
    '江戸前エルフ #01「東京のエルフのはなし」【アニメイズム】',
    'トニカクカワイイ(シーズン2) 第1話「All because of you」',
    '異世界はスマートフォンとともに。2 第1章',
    '山田くんとLv999の恋をする Lv.02「そろそろボス湧きの時間なんで」',
    'アニメA・僕の心のヤバイやつ Karte.1「僕は奪われた」',
    'TVアニメ「アイドルマスター シンデレラガールズ U149」 第1話',
    'テストケース 第1話「うおおお」「うおお」',
    'テストケース (0)',
    'チェンソーマン #11,12 END',
    'アニメ 私の百合はお仕事です! シフト01 ようこそリーベ女学園へ!',
    'PSYCHO-PASS サイコパス 3 一挙放送(2) 第5~8話',
    'アニメ ヤマノススメ Next Summit 最終話 行こう!新しい頂きへ',
    '水曜アニメ・水もん 白聖女と黒牧師 #6アベルとヘーゼリッタ',
    '白聖女と黒牧師 第6話「アベルとヘーゼリッタ」',
    '魔王学院の不適合者Ⅱ 「EPISODE06 母なる大精霊と魔王の右腕」',
    '無職転生Ⅱ ~異世界行ったら本気だす~(第六話)',
    '「もののがたり」第二章 #19「波濤(はとう)」',
    '新世紀エヴァンゲリオン 全26話一挙放送 #1-9',
    '異世界でチート能力を手にした俺は 一挙放送 #8-13',
    '『マイホームヒーロー』イッキ見#9~10【佐々木蔵之介×高橋恭平】',
    '16bitセンセーション ANOTHER LAYERLayer 13「わたしの大切なもの」',
    'Fate/Zero 第十七話「第八の契約」',
    '文豪ストレイドッグス わん！　＃01「第1わん！」',
    'ざつ旅-That\'s Journey- 第1旅「はじめの1225段」',
    'アニメギルド 暗殺者である俺のステータスが勇者よりも明らかに強いのだが #1',
    '最後にひとつだけお願いしてもよろしいでしょうか EP10',
    'アニメ 追放された転生重騎士はゲーム知識で無双する Chapter 2',
    'アニメ ヘルモード ~やり込み好きのゲーマーは廃設定の異世界で無双する~2 14',
    '無職転生III ~異世界行ったら本気だす~ 第1・2話【エリス修行編】特別連続放送',
]


class ProgramTitleParserTest(unittest.TestCase):

    def test_all_reference_titles_match(self) -> None:
        for title in REFERENCE_TITLES:
            with self.subTest(title=title):
                self.assertIsNotNone(ProgramTitleParser.parse(title, DEFAULT_PROGRAM_TITLE_REGEX))


    def test_representative_capture_values(self) -> None:
        expected_results = [
            (
                'アリス・ギア・アイギス Expansion #01「さらば成子坂製作所!」',
                'アリス・ギア・アイギス Expansion',
                '01',
                'さらば成子坂製作所!',
            ),
            (
                'TVアニメ「アイドルマスター シンデレラガールズ U149」 第1話',
                'アイドルマスター シンデレラガールズ U149',
                '1',
                None,
            ),
            (
                '[字]名探偵コナン #968',
                '名探偵コナン',
                '968',
                None,
            ),
            (
                '[解][字]アニメ　透明男と人間女～そのうち夫婦になるふたり～　第2話 「デート大作戦」',
                '透明男と人間女～そのうち夫婦になるふたり～',
                '2',
                'デート大作戦',
            ),
            (
                '[新][解][字]アニメ　透明男と人間女～そのうち夫婦になるふたり～　#01',
                '透明男と人間女～そのうち夫婦になるふたり～',
                '01',
                None,
            ),
            (
                '[字]アニメ　永久のユウグレ[終]　第12話 あなたの愛はあなたのもの',
                '永久のユウグレ',
                '12',
                'あなたの愛はあなたのもの',
            ),
            (
                '[終]＜ノイタミナ＞しゃばけ　第十三話　[解][字]',
                'しゃばけ',
                '十三',
                None,
            ),
            (
                'アニメA・僕の心のヤバイやつ Karte.1「僕は奪われた」',
                '僕の心のヤバイやつ',
                '1',
                '僕は奪われた',
            ),
            (
                'チェンソーマン #11,12 END',
                'チェンソーマン',
                '11,12',
                'END',
            ),
            (
                'アニメ ヤマノススメ Next Summit 最終話 行こう!新しい頂きへ',
                'ヤマノススメ Next Summit',
                '終',
                '行こう!新しい頂きへ',
            ),
            (
                'アニメ ヘルモード ~やり込み好きのゲーマーは廃設定の異世界で無双する~2 14',
                'ヘルモード ~やり込み好きのゲーマーは廃設定の異世界で無双する~2',
                '14',
                None,
            ),
        ]

        for title, series_title, episode_number, subtitle in expected_results:
            with self.subTest(title=title):
                result = ProgramTitleParser.parse(title, DEFAULT_PROGRAM_TITLE_REGEX)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.series_title, series_title)
                self.assertEqual(result.episode_number, episode_number)
                self.assertEqual(result.subtitle, subtitle)


    def test_all_known_arib_program_decoration_marks_are_removed_only_for_parsing(self) -> None:
        for mark in ARIB_PROGRAM_DECORATION_MARKS:
            with self.subTest(mark=mark):
                title = f'[{mark}]テストアニメ[{mark}] #1 [{mark}]'
                result = ProgramTitleParser.parse(title, DEFAULT_PROGRAM_TITLE_REGEX)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.series_title, 'テストアニメ')
                self.assertEqual(result.episode_number, '1')
                self.assertIsNone(result.subtitle)


    def test_parenthesized_arib_program_decoration_marks_are_removed(self) -> None:
        for mark in ['二', '字', '再']:
            with self.subTest(mark=mark):
                result = ProgramTitleParser.parse(f'({mark})テストアニメ #1', DEFAULT_PROGRAM_TITLE_REGEX)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.series_title, 'テストアニメ')


    def test_unknown_bracketed_text_is_preserved_as_part_of_series_title(self) -> None:
        result = ProgramTitleParser.parse('[OVA] テストアニメ [Season 2] #1', DEFAULT_PROGRAM_TITLE_REGEX)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.series_title, '[OVA] テストアニメ [Season 2]')


    def test_nonmatching_title_returns_none(self) -> None:
        self.assertIsNone(ProgramTitleParser.parse('話数表記のない単発番組', DEFAULT_PROGRAM_TITLE_REGEX))


    def test_quoted_subtitle_rule_matches_program_without_episode_number(self) -> None:
        result = ProgramTitleParser.parseWithRules(
            '名探偵コナン「黒ずくめの謀略（狩り）」',
            DEFAULT_PROGRAM_TITLE_REGEX_RULES,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_rule_index, 1)
        self.assertEqual(result.series_title, '名探偵コナン')
        self.assertIsNone(result.episode_number)
        self.assertEqual(result.subtitle, '黒ずくめの謀略（狩り）')


    def test_fullwidth_episode_number_and_lecture_unit_are_supported(self) -> None:
        result = ProgramTitleParser.parse('３年Ｚ組銀八先生 第１２講', DEFAULT_PROGRAM_TITLE_REGEX)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.series_title, '３年Ｚ組銀八先生')
        self.assertEqual(result.episode_number, '１２')


    def test_first_matching_enabled_rule_has_priority(self) -> None:
        rules = [
            ProgramTitleRegexRule(name='優先ルール', pattern=r'^(名探偵コナン)「()(.*)」$'),
            ProgramTitleRegexRule(name='後続ルール', pattern=r'^(.+?)「()(.*)」$'),
        ]
        result = ProgramTitleParser.parseWithRules('名探偵コナン「テスト」', rules)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_rule_index, 0)
        self.assertEqual(result.series_title, '名探偵コナン')


    def test_disabled_rule_is_skipped(self) -> None:
        rules = [
            ProgramTitleRegexRule(name='無効ルール', pattern=r'^(名探偵コナン)「()(.*)」$', enabled=False),
            ProgramTitleRegexRule(name='有効ルール', pattern=r'^(.+?)「()(.*)」$'),
        ]
        result = ProgramTitleParser.parseWithRules('名探偵コナン「テスト」', rules)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_rule_index, 1)


    def test_all_rules_cannot_be_disabled(self) -> None:
        with self.assertRaises(ValueError):
            ProgramTitleParser.validateRules([
                ProgramTitleRegexRule(name='無効ルール', pattern=r'(.+)', enabled=False),
            ])


    def test_unanchored_custom_pattern_searches_the_whole_title(self) -> None:
        result = ProgramTitleParser.parse('放送枠: テストアニメ #12', r'放送枠:\s*(.+?)\s*#(\d+)')
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.series_title, 'テストアニメ')
        self.assertEqual(result.episode_number, '12')


    def test_pattern_requires_a_capture_group(self) -> None:
        with self.assertRaises(ValueError):
            ProgramTitleParser.validatePattern(r'番組タイトル')


    def test_invalid_pattern_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProgramTitleParser.validatePattern(r'(.+')


if __name__ == '__main__':
    unittest.main()
