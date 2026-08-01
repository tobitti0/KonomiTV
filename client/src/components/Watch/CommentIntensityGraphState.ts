import { reactive, readonly, ref, watch } from 'vue';

import {
    DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS,
    EMPTY_COMMENT_INTENSITY_STATISTICS,
    ICommentIntensityGraphSettings,
    ICommentIntensityStatistics,
} from '@/components/Watch/CommentIntensityGraphUtils';


/** デバッグ画面から変更できる、書き込み可能なコメント勢いグラフ設定 */
export type ICommentIntensityGraphMutableSettings = {
    -readonly [Key in keyof ICommentIntensityGraphSettings]: ICommentIntensityGraphSettings[Key];
};

/** ON/OFF のみ通常利用時も維持する。デバッグ用の調整値は再読み込み時に既定値へ戻す */
const COMMENT_INTENSITY_GRAPH_ENABLED_STORAGE_KEY = 'KonomiTV-CommentIntensityGraphEnabled';

const loadGraphEnabled = (): boolean => {
    if (typeof window === 'undefined') {
        return true;
    }
    return window.localStorage.getItem(COMMENT_INTENSITY_GRAPH_ENABLED_STORAGE_KEY) !== 'false';
};

// 兄弟コンポーネント間で共有する軽量なモジュールスコープ状態
const is_enabled = ref(loadGraphEnabled());
const settings = reactive<ICommentIntensityGraphMutableSettings>({
    ...DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS,
});
const statistics = ref<ICommentIntensityStatistics>({
    ...EMPTY_COMMENT_INTENSITY_STATISTICS,
});

watch(is_enabled, (enabled) => {
    if (typeof window !== 'undefined') {
        window.localStorage.setItem(COMMENT_INTENSITY_GRAPH_ENABLED_STORAGE_KEY, String(enabled));
    }
});

/** コメント勢いグラフの共有状態を取得する */
export default function useCommentIntensityGraphState() {
    return {
        is_enabled,
        settings,
        statistics: readonly(statistics),
        setStatistics(value: ICommentIntensityStatistics): void {
            statistics.value = value;
        },
        clearStatistics(): void {
            statistics.value = {...EMPTY_COMMENT_INTENSITY_STATISTICS};
        },
        resetSettings(): void {
            Object.assign(settings, DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS);
        },
    };
}
