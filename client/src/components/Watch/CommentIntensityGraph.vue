<template>
    <div class="comment-intensity-graph" :class="{'comment-intensity-graph--display': should_display}"
        aria-hidden="true">
        <div class="comment-intensity-graph__label">
            <span class="comment-intensity-graph__label-dot"></span>
            <span>コメント勢い</span>
        </div>
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

import { computed, onBeforeUnmount, ref, watch } from 'vue';

import type { PlayerEvents } from '@/stores/PlayerStore';

import {
    buildCommentIntensityGraphPaths,
    calculateCommentIntensity,
    COMMENT_INTENSITY_GRAPH_HEIGHT,
    COMMENT_INTENSITY_GRAPH_WIDTH,
} from '@/components/Watch/CommentIntensityGraphUtils';
import usePlayerStore from '@/stores/PlayerStore';

// Store の初期化
const playerStore = usePlayerStore();

// 集計済みのコメント勢い
// コメントが大量にあっても、ここには最大約360点しか保持しない
const intensity_values = ref<number[]>([]);

// SVG に渡す折れ線と塗りつぶし領域のパス
const graph_paths = computed(() => buildCommentIntensityGraphPaths(intensity_values.value));

// コメント勢いが取得済みで、プレイヤーのコントロールが表示されている間だけグラフを表示する
const should_display = computed(() => {
    return intensity_values.value.length > 0 && playerStore.is_control_display;
});

// PlayerController が既存の過去ログコメントを取得したタイミングで、一度だけ勢いを集計する
const handleCommentReceived = (event: PlayerEvents['CommentReceived']): void => {
    if (event.is_initial_comments === false) {
        return;
    }
    intensity_values.value = calculateCommentIntensity(
        event.comments,
        playerStore.recorded_program.recorded_video.duration,
    );
};
playerStore.event_emitter.on('CommentReceived', handleCommentReceived);

// 録画番組の切り替え直後に前の番組のグラフが一瞬残らないよう、番組 ID の変更時に表示内容を消去する
watch(
    () => playerStore.recorded_program.id,
    () => {
        intensity_values.value = [];
    },
);

// コンポーネントの破棄時は、自身が登録したイベントハンドラーだけを解除する
onBeforeUnmount(() => {
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

    &__label {
        display: flex;
        position: absolute;
        top: 1px;
        left: 1px;
        align-items: center;
        color: rgba(255, 255, 255, 0.84);
        font-size: 10px;
        font-weight: 500;
        line-height: 1;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.9);
        letter-spacing: 0.04em;
    }

    &__label-dot {
        width: 5px;
        height: 5px;
        margin-right: 4px;
        background: rgb(var(--v-theme-primary));
        border-radius: 50%;
        box-shadow: 0 0 5px rgb(var(--v-theme-primary));
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

        &__label {
            display: none;
        }
    }
}

</style>
