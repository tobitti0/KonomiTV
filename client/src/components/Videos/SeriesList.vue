<template>
    <section class="series-list" :class="`series-list--${layout.toLowerCase()}`">
        <div class="series-list__header">
            <div class="series-list__heading">
                <h2>{{title}}</h2>
                <span v-if="!showMoreButton && !isLoading">{{total}}件</span>
            </div>
            <v-btn v-if="showMoreButton" variant="text" class="series-list__more" @click="$emit('more')">
                <span class="text-primary">もっと見る</span>
                <Icon icon="fluent:chevron-right-12-regular" width="18px" class="ml-1 text-text-darken-1" />
            </v-btn>
        </div>

        <div v-if="isLoading" class="series-list__grid">
            <div v-for="index in 6" :key="`series-skeleton-${index}`" class="series-list__skeleton">
                <div class="series-list__skeleton-thumbnail"></div>
                <div class="series-list__skeleton-text"></div>
                <div class="series-list__skeleton-text series-list__skeleton-text--short"></div>
            </div>
        </div>
        <div v-else-if="seriesList.length > 0" class="series-list__grid">
            <SeriesCard v-for="series in seriesList" :key="series.id" :series="series" />
        </div>
        <div v-else-if="showEmptyMessage" class="series-list__empty">
            <Icon icon="fluent:video-clip-multiple-16-regular" width="52px" />
            <h3>{{emptyMessage}}</h3>
            <p>{{emptySubMessage}}</p>
        </div>

        <v-pagination v-if="!hidePagination && total > 0" class="series-list__pagination"
            v-model="currentPage" active-color="primary" density="comfortable"
            :length="Math.ceil(total / 30)" :total-visible="Utils.isSmartphoneVertical() ? 5 : 7"
            @update:model-value="$emit('update:page', $event)" />
    </section>
</template>
<script lang="ts" setup>

import { ref, watch } from 'vue';

import SeriesCard from '@/components/Videos/SeriesCard.vue';
import { ISeries } from '@/services/Series';
import Utils from '@/utils';

const props = withDefaults(defineProps<{
    title: string;
    seriesList: ISeries[];
    total: number;
    page?: number;
    layout?: 'Carousel' | 'Grid';
    hidePagination?: boolean;
    showMoreButton?: boolean;
    showEmptyMessage?: boolean;
    emptyMessage?: string;
    emptySubMessage?: string;
    isLoading?: boolean;
}>(), {
    page: 1,
    layout: 'Grid',
    hidePagination: false,
    showMoreButton: false,
    showEmptyMessage: true,
    emptyMessage: 'シリーズが見つかりませんでした。',
    emptySubMessage: 'シリーズ再判定を実行するか、別のキーワードで検索してください。',
    isLoading: false,
});

defineEmits<{
    (e: 'update:page', page: number): void;
    (e: 'more'): void;
}>();

const currentPage = ref(props.page);
watch(() => props.page, (page) => {
    currentPage.value = page;
});

</script>
<style lang="scss" scoped>

.series-list {
    min-width: 0;

    &__header {
        display: flex;
        align-items: center;
        min-width: 0;
        padding-top: 8px;
        padding-bottom: 18px;
        @include smartphone-vertical {
            padding: 8px 8px 15px;
        }
    }

    &__heading {
        display: flex;
        align-items: baseline;
        min-width: 0;

        h2 {
            overflow: hidden;
            font-size: 24px;
            font-weight: 700;
            text-overflow: ellipsis;
            white-space: nowrap;
            @include smartphone-vertical {
                font-size: 22px;
            }
        }

        span {
            flex-shrink: 0;
            margin-left: 12px;
            color: rgb(var(--v-theme-text-darken-1));
            font-size: 14px;
        }
    }

    &__more {
        flex-shrink: 0;
        margin-left: auto;
        padding: 0 6px 0 12px;
        font-size: 15px;
    }

    &__grid {
        display: grid;
        gap: 16px;
        min-width: 0;
    }

    &--grid &__grid {
        grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
        @include smartphone-vertical {
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }
    }

    &--carousel &__grid {
        grid-auto-columns: minmax(220px, 28%);
        grid-auto-flow: column;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 3px 1px 12px;
        scroll-snap-type: x proximity;

        > * {
            scroll-snap-align: start;
        }

        @include tablet-vertical {
            grid-auto-columns: minmax(210px, 38%);
        }
        @include smartphone-horizontal {
            grid-auto-columns: minmax(190px, 42%);
        }
        @include smartphone-vertical {
            grid-auto-columns: minmax(170px, 72%);
            gap: 10px;
            margin: 0 8px;
        }
    }

    &__skeleton {
        overflow: hidden;
        border-radius: 9px;
        background: rgb(var(--v-theme-background-lighten-1));

        &-thumbnail, &-text {
            background: linear-gradient(100deg,
                rgb(var(--v-theme-background-lighten-1)) 20%,
                rgb(var(--v-theme-background-lighten-2)) 45%,
                rgb(var(--v-theme-background-lighten-1)) 70%);
            background-size: 220% 100%;
            animation: series-skeleton 1.35s linear infinite;
        }

        &-thumbnail {
            aspect-ratio: 16 / 9;
        }

        &-text {
            width: calc(100% - 26px);
            height: 14px;
            margin: 13px;
            border-radius: 5px;

            &--short {
                width: 48%;
                margin-top: -5px;
            }
        }
    }

    &__empty {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        min-height: 235px;
        padding: 30px 18px;
        border-radius: 9px;
        text-align: center;
        background: rgb(var(--v-theme-background-lighten-1));
        color: rgb(var(--v-theme-text-darken-1));

        h3 {
            margin-top: 13px;
            color: rgb(var(--v-theme-text));
            font-size: 20px;
        }

        p {
            margin-top: 8px;
            font-size: 14px;
        }
    }

    &__pagination {
        margin-top: 24px;
    }
}

@keyframes series-skeleton {
    from { background-position: 100% 0; }
    to { background-position: -120% 0; }
}

</style>
