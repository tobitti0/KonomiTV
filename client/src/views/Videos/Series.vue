<template>
    <div class="route-container">
        <HeaderBar />
        <main>
            <Navigation />
            <div class="series-page-container-wrapper">
                <SPHeaderBar />
                <div class="series-page-container">
                    <Breadcrumbs :crumbs="[
                        { name: 'ホーム', path: '/' },
                        { name: 'ビデオをみる', path: '/videos/' },
                        { name: 'シリーズ', path: '/videos/series', disabled: true },
                    ]" />

                    <form class="series-page__conditions" @submit.prevent="applyConditions">
                        <div class="series-page__search">
                            <v-text-field v-model="searchQueryInput" color="primary" bg-color="background-lighten-1"
                                variant="solo" density="comfortable" hide-details clearable
                                placeholder="シリーズ名や概要から検索">
                                <template #prepend-inner>
                                    <Icon icon="fluent:search-20-filled" width="20px" />
                                </template>
                            </v-text-field>
                        </div>
                        <div class="series-page__filters">
                            <v-select v-model="sortOptionInput" class="series-page__sort" :items="sortOptions"
                                color="primary" bg-color="background-lighten-1" variant="solo" density="comfortable"
                                label="並び替え" hide-details />
                            <div class="series-page__date-range">
                                <v-text-field v-model="broadcastStartDateInput" type="date" color="primary"
                                    bg-color="background-lighten-1" variant="solo" density="comfortable"
                                    label="放送日（開始）" hide-details clearable />
                                <span>〜</span>
                                <v-text-field v-model="broadcastEndDateInput" type="date" color="primary"
                                    bg-color="background-lighten-1" variant="solo" density="comfortable"
                                    label="放送日（終了）" hide-details clearable />
                            </div>
                            <div class="series-page__condition-actions">
                                <v-btn type="submit" color="primary" variant="flat" height="48px">
                                    <Icon icon="fluent:filter-20-filled" width="20px" />
                                    <span class="ml-2">適用</span>
                                </v-btn>
                                <v-btn v-if="hasConditionInput" type="button" variant="text" height="48px"
                                    @click="resetConditions">
                                    リセット
                                </v-btn>
                            </div>
                        </div>
                    </form>

                    <SeriesList :title="appliedQuery === '' ? 'シリーズ' : `「${appliedQuery}」のシリーズ検索結果`"
                        :seriesList="seriesList" :total="totalSeries" :page="currentPage"
                        :isLoading="isLoading" @update:page="updatePage" />
                </div>
            </div>
        </main>
    </div>
</template>
<script lang="ts" setup>

import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import SeriesList from '@/components/Videos/SeriesList.vue';
import Message from '@/message';
import SeriesService, { ISeries, SeriesSort } from '@/services/Series';
import useUserStore from '@/stores/UserStore';
import { dayjs } from '@/utils';

type SeriesSortOptionValue =
    'broadcasted_at_desc' |
    'broadcasted_at_asc' |
    'updated_at_desc' |
    'title_asc' |
    'title_desc';

const DEFAULT_SORT_OPTION: SeriesSortOptionValue = 'broadcasted_at_desc';
const sortOptions: {title: string; value: SeriesSortOptionValue}[] = [
    { title: '最近放送された順', value: 'broadcasted_at_desc' },
    { title: '放送が古い順', value: 'broadcasted_at_asc' },
    { title: '最近更新された順', value: 'updated_at_desc' },
    { title: 'タイトル順（昇順）', value: 'title_asc' },
    { title: 'タイトル順（降順）', value: 'title_desc' },
];

const route = useRoute();
const router = useRouter();

const seriesList = ref<ISeries[]>([]);
const totalSeries = ref(0);
const currentPage = ref(1);
const searchQueryInput = ref('');
const appliedQuery = ref('');
const sortOptionInput = ref<SeriesSortOptionValue>(DEFAULT_SORT_OPTION);
const appliedSortOption = ref<SeriesSortOptionValue>(DEFAULT_SORT_OPTION);
const broadcastStartDateInput = ref('');
const broadcastEndDateInput = ref('');
const appliedBroadcastStartDate = ref('');
const appliedBroadcastEndDate = ref('');
const isLoading = ref(true);
let isMounted = false;

const hasConditionInput = computed(() => {
    return searchQueryInput.value.trim() !== '' ||
        sortOptionInput.value !== DEFAULT_SORT_OPTION ||
        broadcastStartDateInput.value !== '' ||
        broadcastEndDateInput.value !== '';
});

// URL から受け取った並び替え値が選択肢に含まれる場合だけ採用する
const getRouteSortOption = (): SeriesSortOptionValue => {
    const routeSort = typeof route.query.sort === 'string' ? route.query.sort : '';
    return sortOptions.some(option => option.value === routeSort)
        ? routeSort as SeriesSortOptionValue
        : DEFAULT_SORT_OPTION;
};

// YYYY-MM-DD 形式の有効な日付だけを API へ渡す
const getRouteDate = (value: unknown): string => {
    if (typeof value !== 'string' || /^\d{4}-\d{2}-\d{2}$/.test(value) === false) {
        return '';
    }
    return dayjs(value).isValid() && dayjs(value).format('YYYY-MM-DD') === value ? value : '';
};

const getSortParameters = (option: SeriesSortOptionValue): {sort: SeriesSort; order: 'desc' | 'asc'} => {
    const separatorIndex = option.lastIndexOf('_');
    return {
        sort: option.slice(0, separatorIndex) as SeriesSort,
        order: option.slice(separatorIndex + 1) as 'desc' | 'asc',
    };
};

// 適用済み条件を URL クエリへ変換し、履歴移動・再読み込みでも同じ一覧を復元できるようにする
const buildRouteQuery = (
    page: number,
    query: string,
    sortOption: SeriesSortOptionValue,
    broadcastStartDate: string,
    broadcastEndDate: string,
) => ({
    ...(query !== '' ? {query} : {}),
    ...(sortOption !== DEFAULT_SORT_OPTION ? {sort: sortOption} : {}),
    ...(broadcastStartDate !== '' ? {broadcast_start_date: broadcastStartDate} : {}),
    ...(broadcastEndDate !== '' ? {broadcast_end_date: broadcastEndDate} : {}),
    page: page.toString(),
});

// URL の検索条件・ページ番号を反映し、シリーズ一覧を取得する
const fetchSeries = async () => {
    appliedQuery.value = typeof route.query.query === 'string' ? route.query.query : '';
    searchQueryInput.value = appliedQuery.value;
    appliedSortOption.value = getRouteSortOption();
    sortOptionInput.value = appliedSortOption.value;
    appliedBroadcastStartDate.value = getRouteDate(route.query.broadcast_start_date);
    broadcastStartDateInput.value = appliedBroadcastStartDate.value;
    appliedBroadcastEndDate.value = getRouteDate(route.query.broadcast_end_date);
    broadcastEndDateInput.value = appliedBroadcastEndDate.value;
    currentPage.value = typeof route.query.page === 'string' ? Math.max(1, Number.parseInt(route.query.page, 10) || 1) : 1;
    isLoading.value = true;

    const {sort, order} = getSortParameters(appliedSortOption.value);
    const options = {
        sort,
        order,
        page: currentPage.value,
        ...(appliedBroadcastStartDate.value !== '' ? {
            broadcast_start_date: appliedBroadcastStartDate.value,
        } : {}),
        ...(appliedBroadcastEndDate.value !== '' ? {
            broadcast_end_date: appliedBroadcastEndDate.value,
        } : {}),
    };
    const result = appliedQuery.value === ''
        ? await SeriesService.fetchSeriesList(options)
        : await SeriesService.searchSeries(appliedQuery.value, options);
    if (result !== null) {
        seriesList.value = result.series_list;
        totalSeries.value = result.total;
    }
    isLoading.value = false;
};

// 入力中の検索・並び替え・放送日条件を URL に反映する
const applyConditions = async () => {
    const query = searchQueryInput.value.trim();
    const broadcastStartDate = broadcastStartDateInput.value ?? '';
    const broadcastEndDate = broadcastEndDateInput.value ?? '';
    if (
        broadcastStartDate !== '' &&
        broadcastEndDate !== '' &&
        dayjs(broadcastStartDate).isAfter(dayjs(broadcastEndDate), 'day')
    ) {
        Message.error('放送日の開始日は、終了日以前の日付を指定してください。');
        return;
    }
    await router.push({
        path: '/videos/series',
        query: buildRouteQuery(1, query, sortOptionInput.value, broadcastStartDate, broadcastEndDate),
    });
};

// 検索・並び替え・放送日条件を初期値へ戻して一覧を再取得する
const resetConditions = async () => {
    searchQueryInput.value = '';
    sortOptionInput.value = DEFAULT_SORT_OPTION;
    broadcastStartDateInput.value = '';
    broadcastEndDateInput.value = '';
    await router.push({path: '/videos/series', query: {page: '1'}});
};

const updatePage = async (page: number) => {
    await router.push({
        path: '/videos/series',
        query: buildRouteQuery(
            page,
            appliedQuery.value,
            appliedSortOption.value,
            appliedBroadcastStartDate.value,
            appliedBroadcastEndDate.value,
        ),
    });
};

watch(() => route.query, async () => {
    if (isMounted === true) {
        await fetchSeries();
    }
}, { deep: true });

onMounted(async () => {
    await useUserStore().fetchUser();
    isMounted = true;
    await fetchSeries();
});

</script>
<style lang="scss" scoped>

.series-page-container-wrapper {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
}

.series-page-container {
    width: 100%;
    max-width: 1120px;
    padding: 20px;
    margin: 0 auto;
    @include smartphone-horizontal {
        padding: 16px 20px;
    }
    @include smartphone-vertical {
        padding: 8px;
    }
}

.series-page__conditions {
    padding: 14px;
    margin: 4px 0 22px;
    border-radius: 9px;
    background: rgb(var(--v-theme-background-lighten-1));
    @include smartphone-vertical {
        margin: 4px 8px 18px;
        padding: 10px;
    }
}

.series-page__search {
    display: flex;
    max-width: 720px;
}

.series-page__filters {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 10px;
    @include tablet-vertical {
        align-items: stretch;
        flex-wrap: wrap;
    }
    @include smartphone-vertical {
        flex-direction: column;
    }
}

.series-page__sort {
    flex: 0 1 245px;
    min-width: 210px;
    @include smartphone-vertical {
        flex-basis: auto;
        width: 100%;
    }
}

.series-page__date-range {
    display: flex;
    align-items: center;
    flex: 1 1 430px;
    gap: 8px;
    min-width: 350px;
    @include smartphone-vertical {
        flex-basis: auto;
        min-width: 0;
        width: 100%;
    }

    > span {
        flex-shrink: 0;
        color: rgb(var(--v-theme-text-darken-1));
    }
}

.series-page__condition-actions {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    @include smartphone-vertical {
        justify-content: flex-end;
        width: 100%;
    }
}

</style>
