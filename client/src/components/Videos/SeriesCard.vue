<template>
    <router-link v-ripple class="series-card" :to="`/videos/series/${series.id}`">
        <div class="series-card__thumbnail">
            <img v-if="representativeProgram !== null" class="series-card__thumbnail-image" loading="lazy" decoding="async"
                :src="`${Utils.api_base_url}/videos/${representativeProgram.id}/thumbnail`">
            <div v-else class="series-card__thumbnail-empty">
                <Icon icon="fluent:video-clip-multiple-16-regular" width="42px" />
            </div>
            <div class="series-card__thumbnail-gradient"></div>
            <div class="series-card__episode-count">{{programCount}}本</div>
        </div>
        <div class="series-card__content">
            <h3 class="series-card__title">{{series.title}}</h3>
            <div class="series-card__meta">
                <span>{{broadcastPeriodCount}}放送期間</span>
                <span v-if="latestProgram !== null">最新 {{dayjs(latestProgram.start_time).format('YYYY/M/D')}}</span>
            </div>
            <div class="series-card__description">{{series.description}}</div>
        </div>
    </router-link>
</template>
<script lang="ts" setup>

import { computed } from 'vue';

import { ISeries } from '@/services/Series';
import Utils, { dayjs, SeriesUtils } from '@/utils';

const props = defineProps<{
    series: ISeries;
}>();

const allPrograms = computed(() => SeriesUtils.getAllPrograms(props.series));

// カードの代表画像には、シリーズ内で最も新しい再生可能な録画番組を利用する
const representativeProgram = computed(() => SeriesUtils.getRepresentativeProgram(props.series));
const latestProgram = computed(() => allPrograms.value[allPrograms.value.length - 1] ?? null);
const programCount = computed(() => allPrograms.value.length);
const broadcastPeriodCount = computed(() => props.series.broadcast_periods.length);

</script>
<style lang="scss" scoped>

.series-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    overflow: hidden;
    border-radius: 9px;
    color: rgb(var(--v-theme-text));
    background: rgb(var(--v-theme-background-lighten-1));
    text-decoration: none;
    transition: transform 0.18s ease, background-color 0.18s ease;
    user-select: none;

    &:hover {
        transform: translateY(-3px);
        background: rgb(var(--v-theme-background-lighten-2));
    }

    &__thumbnail {
        position: relative;
        width: 100%;
        aspect-ratio: 16 / 9;
        overflow: hidden;
        background: linear-gradient(145deg, rgb(var(--v-theme-background-lighten-2)), rgb(var(--v-theme-black)));

        &-image {
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: transform 0.25s ease;
        }

        &-empty {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            color: rgb(var(--v-theme-text-darken-2));
        }

        &-gradient {
            position: absolute;
            inset: 0;
            background: linear-gradient(to top, rgba(var(--v-theme-black), 0.55), transparent 55%);
        }
    }

    &:hover &__thumbnail-image {
        transform: scale(1.035);
    }

    &__episode-count {
        position: absolute;
        right: 8px;
        bottom: 7px;
        padding: 3px 7px;
        border-radius: 5px;
        font-size: 12px;
        font-weight: 700;
        background: rgba(var(--v-theme-black), 0.78);
    }

    &__content {
        min-width: 0;
        padding: 12px 13px 14px;
    }

    &__title {
        overflow: hidden;
        font-size: 16px;
        font-weight: 700;
        line-height: 1.45;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    &__meta {
        display: flex;
        gap: 9px;
        margin-top: 6px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12px;
    }

    &__description {
        display: -webkit-box;
        min-height: 37px;
        margin-top: 8px;
        overflow: hidden;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12px;
        line-height: 1.55;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
    }
}

</style>
