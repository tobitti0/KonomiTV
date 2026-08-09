<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <div class="series-detail-container-wrapper">
                <SPHeaderBar />
                <div v-if="series !== null" class="series-detail-container">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: 'ビデオをみる', path: '/videos/' },
                        { name: 'シリーズ', path: '/videos/series' },
                        { name: series.title, path: `/videos/series/${series.id}`, disabled: true },
                    ]" />

                    <section class="series-detail__hero">
                        <img v-if="representativeProgram !== null" class="series-detail__hero-image"
                            :src="`${Utils.api_base_url}/videos/${representativeProgram.id}/thumbnail`">
                        <div class="series-detail__hero-shade"></div>
                        <div class="series-detail__hero-content">
                            <div class="series-detail__eyebrow">SERIES</div>
                            <h1>{{series.title}}</h1>
                            <div class="series-detail__metadata">
                                <span>{{allPrograms.length}}本</span>
                                <span>{{series.broadcast_periods.length}}放送期間</span>
                                <span v-if="representativeProgram !== null">
                                    {{dayjs(allPrograms[0]?.start_time).format('YYYY年')}}〜{{dayjs(representativeProgram.start_time).format('YYYY年')}}
                                </span>
                            </div>
                            <p>{{series.description}}</p>
                            <div v-if="series.genres.length > 0" class="series-detail__genres">
                                <span v-for="genre in series.genres" :key="`${genre.major}-${genre.middle}`">
                                    {{genre.major}} / {{genre.middle}}
                                </span>
                            </div>
                            <v-btn v-if="continueProgram !== null" color="primary" variant="flat" size="large"
                                :to="`/videos/watch/${continueProgram.id}`">
                                <Icon icon="fluent:play-20-filled" width="21px" />
                                <span class="ml-2">{{continueButtonLabel}}</span>
                            </v-btn>
                        </div>
                    </section>

                    <section v-if="sortedBroadcastPeriods.length > 0" class="series-detail__episodes">
                        <div class="series-detail__episodes-header">
                            <div>
                                <div class="series-detail__episodes-label">エピソード</div>
                                <h2>{{selectedBroadcastPeriod?.channel.name}}</h2>
                            </div>
                            <v-select v-if="sortedBroadcastPeriods.length > 1" v-model="selectedBroadcastPeriodIndex"
                                :items="broadcastPeriodItems" item-title="title" item-value="value"
                                color="primary" bg-color="background-lighten-1" variant="solo"
                                density="comfortable" hide-details />
                        </div>
                        <div v-if="selectedBroadcastPeriod !== null" class="series-detail__period-meta">
                            {{SeriesUtils.getBroadcastPeriodLabel(selectedBroadcastPeriod)}}
                        </div>
                        <RecordedProgramList title="エピソード" :programs="selectedPrograms"
                            :total="selectedPrograms.length" :hideHeader="true" :hideSort="true"
                            :hidePagination="true" :showEmptyMessage="true" />
                    </section>
                </div>
                <div v-else class="series-detail__loading">
                    <Icon icon="line-md:loading-twotone-loop" width="42px" />
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import RecordedProgramList from '@/components/Videos/RecordedProgramList.vue';
import SeriesService, { ISeries } from '@/services/Series';
import { IRecordedProgram } from '@/services/Videos';
import useSettingsStore from '@/stores/SettingsStore';
import Utils, { dayjs, SeriesUtils } from '@/utils';

const route = useRoute();
const router = useRouter();
const settingsStore = useSettingsStore();

const series = ref<ISeries | null>(null);
const selectedBroadcastPeriodIndex = ref(0);

const sortedBroadcastPeriods = computed(() => {
    return series.value !== null ? SeriesUtils.getSortedBroadcastPeriods(series.value) : [];
});
const selectedBroadcastPeriod = computed(() => sortedBroadcastPeriods.value[selectedBroadcastPeriodIndex.value] ?? null);
const selectedPrograms = computed(() => {
    return selectedBroadcastPeriod.value !== null
        ? SeriesUtils.getProgramsInBroadcastPeriod(selectedBroadcastPeriod.value)
        : [];
});
const allPrograms = computed(() => series.value !== null ? SeriesUtils.getAllPrograms(series.value) : []);
const representativeProgram = computed(() => series.value !== null ? SeriesUtils.getRepresentativeProgram(series.value) : null);
const broadcastPeriodItems = computed(() => sortedBroadcastPeriods.value.map((broadcastPeriod, index) => ({
    title: SeriesUtils.getBroadcastPeriodLabel(broadcastPeriod),
    value: index,
})));

// 視聴途中の番組を優先し、なければ未視聴の先頭番組、全話視聴済みなら第1話を再生候補にする
const continueProgram = computed<IRecordedProgram | null>(() => {
    const playablePrograms = allPrograms.value.filter(program => program.recorded_video.status === 'Recorded');
    const partiallyWatchedProgram = [...playablePrograms].sort((a, b) => {
        const aHistory = settingsStore.settings.watched_history.find(history => history.video_id === a.id);
        const bHistory = settingsStore.settings.watched_history.find(history => history.video_id === b.id);
        return (bHistory?.updated_at ?? 0) - (aHistory?.updated_at ?? 0);
    }).find((program) => {
        const history = settingsStore.settings.watched_history.find(item => item.video_id === program.id);
        return history !== undefined &&
            history.last_playback_position >= 30 &&
            history.last_playback_position < program.recorded_video.duration - 30;
    });
    if (partiallyWatchedProgram !== undefined) {
        return partiallyWatchedProgram;
    }

    const unwatchedProgram = playablePrograms.find((program) => {
        return settingsStore.settings.watched_history.some(history => history.video_id === program.id) === false;
    });
    return unwatchedProgram ?? playablePrograms[0] ?? null;
});
const continueButtonLabel = computed(() => {
    if (continueProgram.value === null) return '再生';
    const history = settingsStore.settings.watched_history.find(item => item.video_id === continueProgram.value?.id);
    return history !== undefined && history.last_playback_position >= 30 ? '続きから再生' : '最初から再生';
});

// ルートが別シリーズへ切り替わった場合も同じコンポーネントで内容を更新する
watch(() => route.params.series_id, async (seriesId) => {
    series.value = null;
    selectedBroadcastPeriodIndex.value = 0;
    const fetchedSeries = await SeriesService.fetchSeries(Number.parseInt(seriesId as string, 10));
    if (fetchedSeries === null) {
        await router.replace('/not-found/');
        return;
    }
    series.value = fetchedSeries;
}, { immediate: true });

</script>
<style lang="scss" scoped>

.series-detail-container-wrapper {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
}

.series-detail-container {
    width: 100%;
    max-width: 1120px;
    padding: 20px;
    margin: 0 auto;
    @include smartphone-vertical {
        padding: 8px;
    }
}

.series-detail__hero {
    position: relative;
    min-height: 410px;
    overflow: hidden;
    border-radius: 12px;
    background: linear-gradient(140deg, rgb(var(--v-theme-background-lighten-2)), rgb(var(--v-theme-black)));
    @include tablet-vertical {
        min-height: 380px;
    }
    @include smartphone-vertical {
        min-height: 440px;
        border-radius: 9px;
    }

    &-image {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        object-fit: cover;
        object-position: center;
        @include smartphone-vertical {
            height: 52%;
        }
    }

    &-shade {
        position: absolute;
        inset: 0;
        background: linear-gradient(90deg, rgba(var(--v-theme-black), 0.97) 0%, rgba(var(--v-theme-black), 0.82) 42%,
            rgba(var(--v-theme-black), 0.22) 75%, rgba(var(--v-theme-black), 0.48) 100%);
        @include smartphone-vertical {
            background: linear-gradient(0deg, rgb(var(--v-theme-black)) 38%, rgba(var(--v-theme-black), 0.08) 76%,
                rgba(var(--v-theme-black), 0.2) 100%);
        }
    }

    &-content {
        position: relative;
        display: flex;
        align-items: flex-start;
        justify-content: center;
        flex-direction: column;
        width: min(590px, 65%);
        min-height: 410px;
        padding: 42px 48px;
        z-index: 1;
        @include tablet-vertical {
            min-height: 380px;
            padding: 36px;
        }
        @include smartphone-vertical {
            justify-content: flex-end;
            width: 100%;
            min-height: 440px;
            padding: 24px 20px;
        }

        h1 {
            font-size: clamp(30px, 4vw, 48px);
            line-height: 1.22;
            letter-spacing: 0.015em;
        }

        p {
            display: -webkit-box;
            margin-top: 17px;
            overflow: hidden;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 15px;
            line-height: 1.75;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 4;
        }

        .v-btn {
            margin-top: 24px;
        }
    }
}

.series-detail__eyebrow {
    margin-bottom: 9px;
    color: rgb(var(--v-theme-primary-lighten-1));
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.22em;
}

.series-detail__metadata, .series-detail__genres {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 13px;
    margin-top: 13px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
}

.series-detail__genres span {
    padding: 4px 8px;
    border-radius: 5px;
    background: rgba(var(--v-theme-background-lighten-2), 0.8);
}

.series-detail__episodes {
    margin-top: 34px;

    &-header {
        display: flex;
        align-items: flex-end;
        gap: 20px;
        margin-bottom: 5px;
        @include smartphone-vertical {
            align-items: stretch;
            flex-direction: column;
            gap: 12px;
            padding: 0 8px;
        }

        h2 {
            margin-top: 3px;
            font-size: 24px;
        }

        .v-select {
            width: min(540px, 58%);
            margin-left: auto;
            @include smartphone-vertical {
                width: 100%;
                margin-left: 0;
            }
        }
    }

    &-label {
        color: rgb(var(--v-theme-primary-lighten-1));
        font-size: 13px;
        font-weight: 700;
    }
}

.series-detail__period-meta {
    margin-bottom: 15px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
    @include smartphone-vertical {
        padding: 0 8px;
    }
}

.series-detail__loading {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    min-height: 55vh;
    color: rgb(var(--v-theme-primary));
}

</style>
