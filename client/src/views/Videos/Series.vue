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

                    <form class="series-page__search" @submit.prevent="applySearch">
                        <v-text-field v-model="searchQueryInput" color="primary" bg-color="background-lighten-1"
                            variant="solo" density="comfortable" hide-details clearable
                            placeholder="シリーズ名や概要から検索">
                            <template #prepend-inner>
                                <Icon icon="fluent:search-20-filled" width="20px" />
                            </template>
                        </v-text-field>
                        <v-btn type="submit" color="primary" variant="flat" height="48px">検索</v-btn>
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

import { onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import Breadcrumbs from '@/components/Breadcrumbs.vue';
import HeaderBar from '@/components/HeaderBar.vue';
import Navigation from '@/components/Navigation.vue';
import SPHeaderBar from '@/components/SPHeaderBar.vue';
import SeriesList from '@/components/Videos/SeriesList.vue';
import SeriesService, { ISeries } from '@/services/Series';
import useUserStore from '@/stores/UserStore';

const route = useRoute();
const router = useRouter();

const seriesList = ref<ISeries[]>([]);
const totalSeries = ref(0);
const currentPage = ref(1);
const searchQueryInput = ref('');
const appliedQuery = ref('');
const isLoading = ref(true);
let isMounted = false;

// URL の検索条件・ページ番号を反映し、シリーズ一覧を取得する
const fetchSeries = async () => {
    appliedQuery.value = typeof route.query.query === 'string' ? route.query.query : '';
    searchQueryInput.value = appliedQuery.value;
    currentPage.value = typeof route.query.page === 'string' ? Math.max(1, Number.parseInt(route.query.page, 10) || 1) : 1;
    isLoading.value = true;

    const result = appliedQuery.value === ''
        ? await SeriesService.fetchSeriesList('desc', currentPage.value)
        : await SeriesService.searchSeries(appliedQuery.value, 'desc', currentPage.value);
    if (result !== null) {
        seriesList.value = result.series_list;
        totalSeries.value = result.total;
    }
    isLoading.value = false;
};

// 入力中の検索条件を URL に反映することで、再読み込みや履歴移動でも同じ結果を復元できるようにする
const applySearch = async () => {
    const query = searchQueryInput.value.trim();
    await router.push({
        path: '/videos/series',
        query: {
            ...(query !== '' ? {query} : {}),
            page: '1',
        },
    });
};

const updatePage = async (page: number) => {
    await router.push({
        path: '/videos/series',
        query: {
            ...(appliedQuery.value !== '' ? {query: appliedQuery.value} : {}),
            page: page.toString(),
        },
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

.series-page__search {
    display: flex;
    gap: 10px;
    max-width: 650px;
    margin: 4px 0 22px;
    @include smartphone-vertical {
        margin: 4px 8px 18px;
    }
}

</style>
