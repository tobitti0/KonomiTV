<template>
    <div class="comment-intensity-graph" :class="{'comment-intensity-graph--display': should_display}"
        aria-hidden="true">
        <svg class="comment-intensity-graph__svg"
            :viewBox="`0 0 ${COMMENT_INTENSITY_GRAPH_WIDTH} ${COMMENT_INTENSITY_GRAPH_HEIGHT}`"
            preserveAspectRatio="none">
            <defs>
                <linearGradient id="comment-intensity-area-gradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="rgb(var(--v-theme-primary))" stop-opacity="0.62" />
                    <stop offset="100%" stop-color="rgb(var(--v-theme-primary))" stop-opacity="0.04" />
                </linearGradient>
            </defs>
            <path class="comment-intensity-graph__area" :d="graph_paths.area_path" />
            <path class="comment-intensity-graph__line" :d="graph_paths.line_path" />
        </svg>
    </div>
</template>
<script setup lang="ts">

import { computed, onBeforeUnmount, shallowRef, watch } from 'vue';

import type { PlayerEvents } from '@/stores/PlayerStore';

import useCommentIntensityGraphState from '@/components/Watch/CommentIntensityGraphState';
import {
    buildCommentIntensityGraphPaths,
    calculateCommentIntensity,
    calculateCommentIntensityStatistics,
    COMMENT_INTENSITY_GRAPH_HEIGHT,
    COMMENT_INTENSITY_GRAPH_WIDTH,
} from '@/components/Watch/CommentIntensityGraphUtils';
import usePlayerStore from '@/stores/PlayerStore';

// Store の初期化
const playerStore = usePlayerStore();
const commentIntensityGraphState = useCommentIntensityGraphState();

// デバッグパネルから設定を変更した際に再集計できるよう、受信済みコメントを保持する
// shallowRef を使い、大量コメントを Vue の深いリアクティブ変換対象にしない
const initial_comments = shallowRef<PlayerEvents['CommentReceived']['comments']>([]);

// コメントまたは設定が変化した時だけ再集計する
const intensity_values = computed(() => calculateCommentIntensity(
    initial_comments.value,
    playerStore.recorded_program.recorded_video.duration,
    commentIntensityGraphState.settings,
));

// コメント描画の初期化を勢い集計で待たせないため、集計は次のイベントループで実行する
let calculation_timer_id: number | null = null;

// SVG に渡す折れ線と塗りつぶし領域のパス
const graph_paths = computed(() => buildCommentIntensityGraphPaths(
    intensity_values.value,
    commentIntensityGraphState.settings,
));

// コメント勢いが取得済みで、プレイヤーのコントロールが表示されている間だけグラフを表示する
const should_display = computed(() => {
    return commentIntensityGraphState.is_enabled.value &&
        intensity_values.value.length > 0 &&
        playerStore.is_control_display;
});

// PlayerController が既存の過去ログコメントを取得したタイミングで、一度だけ勢いを集計する
const handleCommentReceived = (event: PlayerEvents['CommentReceived']): void => {
    if (event.is_initial_comments === false) {
        return;
    }

    // PlayerController はこのイベントの全ハンドラーを同期実行した後に DPlayer へコメントを渡す。
    // 集計自体は軽量だが、コメント件数や将来の実装変更に関係なくコメント描画をブロックしないよう次のタスクへ送る。
    if (calculation_timer_id !== null) {
        window.clearTimeout(calculation_timer_id);
    }
    const recorded_program_id = playerStore.recorded_program.id;
    calculation_timer_id = window.setTimeout(() => {
        calculation_timer_id = null;

        // タイマー実行前に別の録画番組へ切り替わった場合は、古いコメントからグラフを生成しない
        if (playerStore.recorded_program.id !== recorded_program_id) {
            return;
        }
        initial_comments.value = event.comments;
        commentIntensityGraphState.setStatistics(calculateCommentIntensityStatistics(
            event.comments,
            playerStore.recorded_program.recorded_video.duration,
        ));
    }, 0);
};
playerStore.event_emitter.on('CommentReceived', handleCommentReceived);

// 録画番組の切り替え直後に前の番組のグラフが一瞬残らないよう、番組 ID の変更時に表示内容を消去する
watch(
    () => playerStore.recorded_program.id,
    () => {
        if (calculation_timer_id !== null) {
            window.clearTimeout(calculation_timer_id);
            calculation_timer_id = null;
        }
        initial_comments.value = [];
        commentIntensityGraphState.clearStatistics();
    },
);

// コンポーネントの破棄時は、自身が登録したイベントハンドラーだけを解除する
onBeforeUnmount(() => {
    if (calculation_timer_id !== null) {
        window.clearTimeout(calculation_timer_id);
        calculation_timer_id = null;
    }
    playerStore.event_emitter.off('CommentReceived', handleCommentReceived);
});

</script>
<style lang="scss" scoped>

.comment-intensity-graph {
    position: absolute;
    left: calc(68px + 18px);
    right: 18px;
    bottom: 57px;
    height: 54px;
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.3s ease, visibility 0.3s ease;
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6));
    pointer-events: none;
    z-index: 4;

    &--display {
        opacity: 1;
        visibility: visible;
    }

    &__svg {
        display: block;
        width: 100%;
        height: 100%;
        overflow: visible;
    }

    &__area {
        fill: url('#comment-intensity-area-gradient');
    }

    &__line {
        fill: none;
        stroke: rgb(var(--v-theme-primary));
        stroke-width: 1.4px;
        stroke-linecap: round;
        stroke-linejoin: round;
        vector-effect: non-scaling-stroke;
    }

    @include tablet-vertical {
        left: 18px;
        bottom: 54px;
        height: 44px;
    }

    @include smartphone-horizontal {
        left: 18px;
        bottom: 54px;
        height: 42px;
    }

    @include smartphone-vertical {
        left: 0;
        right: 0;
        bottom: 0;
        height: 34px;
    }
}

</style>
