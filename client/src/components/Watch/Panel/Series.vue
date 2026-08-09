<template>
    <div class="series-panel">
        <div v-if="playerStore.is_series_loading" class="series-panel__state">
            <Icon icon="line-md:loading-twotone-loop" width="36px" />
            <span>シリーズを読み込んでいます…</span>
        </div>

        <div v-else-if="playerStore.series === null" class="series-panel__state">
            <Icon icon="fluent:video-clip-multiple-16-regular" width="42px" />
            <strong>シリーズ情報がありません</strong>
            <span>この録画番組はシリーズに分類されていません。</span>
        </div>

        <template v-else>
            <header class="series-panel__header">
                <div class="series-panel__eyebrow">SERIES</div>
                <router-link :to="`/videos/series/${playerStore.series.id}`">
                    <h1>{{playerStore.series.title}}</h1>
                    <Icon icon="fluent:open-16-regular" width="18px" />
                </router-link>
                <p>{{playerStore.series.description}}</p>
            </header>

            <v-select v-if="sortedBroadcastPeriods.length > 1" class="series-panel__period-select"
                v-model="selectedBroadcastPeriodIndex" :items="broadcastPeriodItems"
                item-title="title" item-value="value" color="primary" bg-color="background-lighten-1"
                variant="solo" density="compact" hide-details />

            <div v-if="selectedBroadcastPeriod !== null" class="series-panel__period-meta">
                {{SeriesUtils.getBroadcastPeriodLabel(selectedBroadcastPeriod)}}
            </div>

            <div ref="episodesContainer" class="series-panel__episodes">
                <button v-for="program in selectedPrograms" :key="program.id" v-ripple
                    class="series-panel__episode"
                    :class="{
                        'series-panel__episode--current': program.id === playerStore.recorded_program.id,
                        'series-panel__episode--disabled': program.recorded_video.status !== 'Recorded',
                    }"
                    :disabled="program.recorded_video.status !== 'Recorded'"
                    @click="openProgram(program)">
                    <div class="series-panel__episode-thumbnail">
                        <img loading="lazy" decoding="async" :src="`${Utils.api_base_url}/videos/${program.id}/thumbnail`">
                        <div v-if="program.id === playerStore.recorded_program.id" class="series-panel__episode-playing">
                            <Icon icon="fluent:play-12-filled" width="14px" />
                            再生中
                        </div>
                        <div v-if="getWatchProgress(program) > 0" class="series-panel__episode-progress">
                            <div :style="{width: `${getWatchProgress(program)}%`}"></div>
                        </div>
                    </div>
                    <div class="series-panel__episode-content">
                        <div class="series-panel__episode-title">{{SeriesUtils.getEpisodeTitle(program)}}</div>
                        <div class="series-panel__episode-meta">
                            {{dayjs(program.start_time).format('M月D日')}} · {{ProgramUtils.getProgramDuration(program)}}
                        </div>
                        <div class="series-panel__episode-description">{{program.description}}</div>
                    </div>
                </button>
            </div>
        </template>
    </div>
</template>
<script lang="ts" setup>

import { computed, nextTick, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { IRecordedProgram } from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';
import Utils, { dayjs, ProgramUtils, SeriesUtils } from '@/utils';

const router = useRouter();
const playerStore = usePlayerStore();
const settingsStore = useSettingsStore();
const selectedBroadcastPeriodIndex = ref(0);
const episodesContainer = ref<HTMLDivElement | null>(null);

const sortedBroadcastPeriods = computed(() => {
    return playerStore.series !== null ? SeriesUtils.getSortedBroadcastPeriods(playerStore.series) : [];
});
const selectedBroadcastPeriod = computed(() => sortedBroadcastPeriods.value[selectedBroadcastPeriodIndex.value] ?? null);
const selectedPrograms = computed(() => {
    return selectedBroadcastPeriod.value !== null
        ? SeriesUtils.getProgramsInBroadcastPeriod(selectedBroadcastPeriod.value)
        : [];
});
const broadcastPeriodItems = computed(() => sortedBroadcastPeriods.value.map((broadcastPeriod, index) => ({
    title: SeriesUtils.getBroadcastPeriodLabel(broadcastPeriod),
    value: index,
})));

// シリーズや再生番組が切り替わったとき、現在話を含む放送期間を自動選択する
watch([
    () => playerStore.series,
    () => playerStore.recorded_program.id,
], async () => {
    const currentPeriodIndex = sortedBroadcastPeriods.value.findIndex((broadcastPeriod) => {
        return broadcastPeriod.recorded_programs.some(program => program.id === playerStore.recorded_program.id);
    });
    selectedBroadcastPeriodIndex.value = currentPeriodIndex >= 0 ? currentPeriodIndex : 0;

    // 長いシリーズでも現在話がすぐ見えるよう、描画完了後に現在話を表示範囲へスクロールする
    await nextTick();
    episodesContainer.value?.querySelector<HTMLElement>('.series-panel__episode--current')?.scrollIntoView({
        block: 'nearest',
    });
}, { immediate: true });

const openProgram = async (program: IRecordedProgram) => {
    if (program.recorded_video.status !== 'Recorded' || program.id === playerStore.recorded_program.id) {
        return;
    }
    await router.push(`/videos/watch/${program.id}`);
};

const getWatchProgress = (program: IRecordedProgram): number => {
    const history = settingsStore.settings.watched_history.find(item => item.video_id === program.id);
    if (history === undefined || program.recorded_video.duration <= 0) {
        return 0;
    }
    return Math.min(100, Math.max(0, history.last_playback_position / program.recorded_video.duration * 100));
};

</script>
<style lang="scss" scoped>

.series-panel {
    height: 100%;
    padding: 0 14px 18px;
    overflow-y: auto;
    @include tablet-vertical {
        padding: 20px 24px;
    }
    @include smartphone-horizontal {
        padding: 10px 10px 12px;
    }
    @include smartphone-vertical {
        padding: 16px 12px;
    }

    &__state {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        gap: 10px;
        height: 100%;
        min-height: 220px;
        color: rgb(var(--v-theme-text-darken-1));
        text-align: center;

        strong {
            color: rgb(var(--v-theme-text));
            font-size: 18px;
        }

        span {
            font-size: 13px;
        }
    }

    &__header {
        padding: 2px 3px 17px;
        border-bottom: 1px solid rgb(var(--v-theme-background-lighten-2));

        a {
            display: flex;
            align-items: center;
            gap: 8px;
            color: rgb(var(--v-theme-text));
            text-decoration: none;
        }

        h1 {
            overflow: hidden;
            font-size: 21px;
            line-height: 1.4;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        p {
            display: -webkit-box;
            margin-top: 9px;
            overflow: hidden;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 12px;
            line-height: 1.55;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
        }
    }

    &__eyebrow {
        margin-bottom: 4px;
        color: rgb(var(--v-theme-primary-lighten-1));
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.18em;
    }

    &__period-select {
        margin-top: 14px;
        font-size: 12px;
    }

    &__period-meta {
        padding: 13px 3px 9px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11px;
        line-height: 1.5;
    }

    &__episodes {
        display: flex;
        flex-direction: column;
        gap: 7px;
    }

    &__episode {
        display: grid;
        grid-template-columns: 118px minmax(0, 1fr);
        gap: 10px;
        width: 100%;
        padding: 8px;
        border-radius: 7px;
        color: rgb(var(--v-theme-text));
        background: rgb(var(--v-theme-background-lighten-1));
        text-align: left;
        transition: background-color 0.15s;
        cursor: pointer;
        @include smartphone-horizontal {
            grid-template-columns: 96px minmax(0, 1fr);
        }

        &:hover, &--current {
            background: rgb(var(--v-theme-background-lighten-2));
        }

        &--current {
            box-shadow: inset 3px 0 0 rgb(var(--v-theme-primary));
        }

        &--disabled {
            opacity: 0.5;
            cursor: default;
        }

        &-thumbnail {
            position: relative;
            align-self: start;
            width: 100%;
            aspect-ratio: 16 / 9;
            overflow: hidden;
            border-radius: 5px;
            background: rgb(var(--v-theme-background-lighten-2));

            img {
                width: 100%;
                height: 100%;
                object-fit: cover;
            }
        }

        &-playing {
            position: absolute;
            display: flex;
            align-items: center;
            gap: 3px;
            left: 5px;
            bottom: 5px;
            padding: 3px 5px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            background: rgba(var(--v-theme-black), 0.82);
        }

        &-progress {
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            height: 3px;
            background: rgba(var(--v-theme-black), 0.75);

            div {
                height: 100%;
                background: rgb(var(--v-theme-primary));
            }
        }

        &-content {
            min-width: 0;
        }

        &-title {
            overflow: hidden;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.45;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        &-meta {
            margin-top: 4px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 10px;
        }

        &-description {
            display: -webkit-box;
            margin-top: 5px;
            overflow: hidden;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 10px;
            line-height: 1.45;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
            @include smartphone-horizontal {
                -webkit-line-clamp: 1;
            }
        }
    }
}

</style>
