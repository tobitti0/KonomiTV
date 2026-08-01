<template>
    <div v-if="is_debug_mode" class="comment-intensity-debug-panel"
        :class="{'comment-intensity-debug-panel--collapsed': is_collapsed}"
        @click.stop @mousedown.stop @touchstart.stop>
        <button v-if="is_collapsed" class="comment-intensity-debug-panel__collapsed-button"
            type="button" @click="is_collapsed = false">
            勢いグラフ調整
        </button>
        <template v-else>
            <div class="comment-intensity-debug-panel__header">
                <span>勢いグラフ調整</span>
                <button type="button" aria-label="調整パネルを最小化" @click="is_collapsed = true">−</button>
            </div>

            <div class="comment-intensity-debug-panel__content">
                <div class="comment-intensity-debug-panel__statistics">
                    <div>total {{ statistics.total_comments.toLocaleString() }} コメ</div>
                    <div>max {{ Math.round(statistics.max_comments_per_minute).toLocaleString() }} コメ/分</div>
                    <div>avg {{ Math.round(statistics.average_comments_per_minute).toLocaleString() }} コメ/分</div>
                </div>

                <label class="comment-intensity-debug-panel__enable">
                    <input type="checkbox" v-model="is_enabled">
                    <span>グラフを表示</span>
                </label>

                <div class="comment-intensity-debug-panel__effective-value">
                    実効値: {{ effective_bucket_duration_seconds }}秒刻み / {{ effective_point_count }}点
                </div>

                <label class="comment-intensity-debug-panel__setting-row">
                    <span>目標点数</span><output>{{ settings.target_point_count }}</output>
                    <input type="range" min="120" max="720" step="30" v-model.number="settings.target_point_count">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>最小刻み</span><output>{{ settings.minimum_bucket_duration_seconds }}秒</output>
                    <input type="range" min="1" max="30" step="1" v-model.number="settings.minimum_bucket_duration_seconds">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>最大刻み</span><output>{{ settings.maximum_bucket_duration_seconds }}秒</output>
                    <input type="range" min="5" max="120" step="1" v-model.number="settings.maximum_bucket_duration_seconds">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>短期半径</span><output>{{ settings.smoothing_radius }}</output>
                    <input type="range" min="0" max="10" step="1" v-model.number="settings.smoothing_radius">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>短期σ</span><output>{{ settings.smoothing_standard_deviation.toFixed(1) }}</output>
                    <input type="range" min="0.5" max="5" step="0.1" v-model.number="settings.smoothing_standard_deviation">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>長期半径</span><output>{{ settings.baseline_smoothing_radius }}</output>
                    <input type="range" min="1" max="60" step="1" v-model.number="settings.baseline_smoothing_radius">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>長期σ</span><output>{{ settings.baseline_smoothing_standard_deviation.toFixed(1) }}</output>
                    <input type="range" min="0.5" max="30" step="0.5" v-model.number="settings.baseline_smoothing_standard_deviation">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>局所ピーク強調</span><output>{{ settings.relative_peak_amplification.toFixed(2) }}</output>
                    <input type="range" min="0" max="1" step="0.05" v-model.number="settings.relative_peak_amplification">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>局所上昇上限</span><output>{{ settings.maximum_relative_peak_lift.toFixed(1) }}</output>
                    <input type="range" min="0" max="5" step="0.1" v-model.number="settings.maximum_relative_peak_lift">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>表示上限</span><output>{{ (settings.display_maximum_percentile * 100).toFixed(1) }}%</output>
                    <input type="range" min="0.9" max="1" step="0.005" v-model.number="settings.display_maximum_percentile">
                </label>
                <label class="comment-intensity-debug-panel__setting-row">
                    <span>コントラスト</span><output>{{ settings.contrast_exponent.toFixed(2) }}</output>
                    <input type="range" min="0.5" max="3" step="0.05" v-model.number="settings.contrast_exponent">
                </label>

                <button class="comment-intensity-debug-panel__reset" type="button" @click="resetSettings">
                    既定値へ戻す
                </button>
            </div>
        </template>
    </div>
</template>
<script setup lang="ts">

import { computed, ref } from 'vue';
import { useRoute } from 'vue-router';

import useCommentIntensityGraphState from '@/components/Watch/CommentIntensityGraphState';
import usePlayerStore from '@/stores/PlayerStore';

const route = useRoute();
const playerStore = usePlayerStore();
const {
    is_enabled,
    settings,
    statistics,
    resetSettings,
} = useCommentIntensityGraphState();

// URL に ?comment-intensity-debug=1 を付けた時だけ表示し、通常利用時の UI から完全に切り離す
const is_debug_mode = computed(() => route.query['comment-intensity-debug'] === '1');
const is_collapsed = ref(false);

const effective_bucket_duration_seconds = computed(() => {
    const duration_seconds = playerStore.recorded_program.recorded_video.duration;
    const minimum_duration = Math.max(1, settings.minimum_bucket_duration_seconds);
    const maximum_duration = Math.max(minimum_duration, settings.maximum_bucket_duration_seconds);
    return Math.min(
        maximum_duration,
        Math.max(minimum_duration, Math.ceil(duration_seconds / Math.max(1, settings.target_point_count))),
    );
});

const effective_point_count = computed(() => {
    return Math.ceil(
        playerStore.recorded_program.recorded_video.duration /
        effective_bucket_duration_seconds.value,
    );
});

</script>
<style lang="scss" scoped>

.comment-intensity-debug-panel {
    position: absolute;
    top: 72px;
    left: 86px;
    width: 310px;
    max-height: calc(100% - 150px);
    color: #FFFFFF;
    background: rgba(17, 13, 15, 0.94);
    border: 1px solid rgba(230, 79, 151, 0.55);
    border-radius: 8px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
    overflow: hidden;
    z-index: 20;

    &--collapsed {
        width: auto;
        background: transparent;
        border: 0;
        box-shadow: none;
    }

    &__collapsed-button,
    &__reset {
        color: #FFFFFF;
        background: rgba(17, 13, 15, 0.9);
        border: 1px solid rgba(230, 79, 151, 0.65);
        border-radius: 6px;
        cursor: pointer;
    }

    &__collapsed-button {
        padding: 8px 12px;
        font-size: 12px;
    }

    &__header {
        display: flex;
        height: 38px;
        padding: 0 10px 0 13px;
        align-items: center;
        justify-content: space-between;
        background: rgba(230, 79, 151, 0.14);
        font-size: 13px;
        font-weight: 700;

        button {
            width: 28px;
            height: 28px;
            color: #FFFFFF;
            background: transparent;
            border: 0;
            border-radius: 50%;
            font-size: 20px;
            cursor: pointer;
        }
    }

    &__content {
        max-height: calc(100vh - 190px);
        padding: 11px 13px 13px;
        overflow-y: auto;
    }

    &__statistics {
        margin-bottom: 10px;
        padding: 8px 10px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 5px;
        font-size: 12px;
        font-weight: 600;
        line-height: 1.45;
        text-shadow: 0 1px 2px #000000;
    }

    &__enable {
        display: flex;
        margin-bottom: 8px;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        cursor: pointer;

        input {
            accent-color: rgb(var(--v-theme-primary));
        }
    }

    &__effective-value {
        margin-bottom: 10px;
        color: rgba(255, 255, 255, 0.7);
        font-size: 10.5px;
    }

    &__setting-row {
        display: grid;
        grid-template-columns: 1fr 72px;
        margin-bottom: 8px;
        align-items: center;
        column-gap: 8px;
        font-size: 11px;

        output {
            text-align: right;
            color: rgba(255, 255, 255, 0.82);
            font-variant-numeric: tabular-nums;
        }

        input[type='range'] {
            grid-column: 1 / 3;
            width: 100%;
            height: 16px;
            margin: 0;
            accent-color: rgb(var(--v-theme-primary));
            cursor: pointer;
        }
    }

    &__reset {
        width: 100%;
        margin-top: 2px;
        padding: 7px 10px;
        font-size: 11px;
    }

    @include tablet-vertical {
        top: 12px;
        left: 12px;
        max-height: calc(100% - 24px);
    }
}

</style>
