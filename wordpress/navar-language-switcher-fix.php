<?php
/**
 * Plugin Name: Navar Translation Link Fix
 * Description: Direct canonical switching across Persian, Arabic, Tajik, English and Russian without redirect chains.
 * Version: 2.1.0
 */
if (!defined('ABSPATH')) exit;

/* Ordered source IDs. Every source has Arabic then Tajik posts in IDs 502015..502320. */
function navar_translation_source_ids() {
    return array(739,952,963,1002,1013,34963,34967,34969,35526,35530,35536,35854,35857,35865,35871,35876,35880,35883,35888,35891,35894,35896,35899,35901,35903,35907,36103,36116,36124,36133,36139,36163,36180,36545,36550,36558,36568,36586,36594,36606,36677,36691,36735,36751,36763,36774,36780,36790,36811,36819,36827,36853,36862,36883,36891,36897,36904,36912,36921,36932,36941,36950,36957,36963,36968,36975,36980,36984,36992,36996,37000,37004,37017,37032,37037,37095,37108,37120,37124,37133,37157,37174,37184,37194,37204,37214,37219,37226,37251,37259,37286,37310,37320,37329,37336,37344,37353,37360,37367,37378,37387,37404,37415,37422,37427,37436,37447,37460,37472,37478,37490,37495,37500,37510,37518,37524,37531,37546,37551,37556,37565,37573,37580,37586,37593,37599,37605,37614,37621,37628,37635,37646,37653,37663,37670,37676,37688,37697,37703,37711,37719,37724,37733,37738,37751,37762,37772,37779,37787,37797,37819,37829,37859);
}

function navar_fallback_record($post_id) {
    $post_id = absint($post_id);
    if ($post_id < 502015 || $post_id > 502320) return array();
    $offset = $post_id - 502015;
    $index = intdiv($offset, 2);
    $sources = navar_translation_source_ids();
    if (!isset($sources[$index])) return array();
    return array(
        'source' => (int) $sources[$index],
        'language' => ($offset % 2 === 0) ? 'ar-IQ' : 'tg-TJ',
        'arabic' => 502015 + ($index * 2),
        'tajik' => 502016 + ($index * 2),
    );
}

function navar_translation_targets($post_id) {
    $post_id = absint($post_id);
    $fallback = navar_fallback_record($post_id);
    $source_id = absint(get_post_meta($post_id, '_navar_translation_source_id', true));
    if (!$source_id && $fallback) $source_id = (int) $fallback['source'];
    if (!$source_id) $source_id = $post_id;

    $targets = array('fa-IR' => $source_id);
    $source_index = array_search($source_id, navar_translation_source_ids(), true);
    if ($source_index !== false) {
        $arabic_id = 502015 + ((int) $source_index * 2);
        $tajik_id = $arabic_id + 1;
        if (get_post_status($arabic_id) === 'publish') $targets['ar-IQ'] = $arabic_id;
        if (get_post_status($tajik_id) === 'publish') $targets['tg-TJ'] = $tajik_id;
    }

    $ids = get_posts(array(
        'post_type'=>'post','post_status'=>'publish','posts_per_page'=>-1,
        'fields'=>'ids','no_found_rows'=>true,'suppress_filters'=>false,
        'meta_query'=>array(array('key'=>'_navar_translation_source_id','value'=>(string)$source_id,'compare'=>'=')),
    ));
    foreach ($ids as $id) {
        $language = (string) get_post_meta($id, '_navar_translation_language', true);
        if (in_array($language, array('ar-IQ','tg-TJ','en-US','ru-RU'), true)) $targets[$language] = absint($id);
    }
    return $targets;
}

function navar_requested_language($raw) {
    $value = strtolower(sanitize_key(wp_unslash((string) $raw)));
    $map = array(
        'iran'=>'fa-IR','fa'=>'fa-IR','fa-ir'=>'fa-IR','persian'=>'fa-IR',
        'iraq'=>'ar-IQ','iq'=>'ar-IQ','ar'=>'ar-IQ','ar-iq'=>'ar-IQ','arabic'=>'ar-IQ',
        'tajikistan'=>'tg-TJ','tj'=>'tg-TJ','tg'=>'tg-TJ','tg-tj'=>'tg-TJ','tajik'=>'tg-TJ',
        'english'=>'en-US','en'=>'en-US','en-us'=>'en-US',
        'russia'=>'ru-RU','russian'=>'ru-RU','ru'=>'ru-RU','ru-ru'=>'ru-RU',
    );
    return isset($map[$value]) ? $map[$value] : '';
}

function navar_country_value($language) {
    if ($language === 'fa-IR') return 'iran';
    if ($language === 'ar-IQ') return 'iraq';
    if ($language === 'tg-TJ') return 'tajikistan';
    if ($language === 'en-US') return 'english';
    if ($language === 'ru-RU') return 'russia';
    return '';
}

add_action('template_redirect', function () {
    if (!is_singular('post') || !isset($_GET['navar_lang'])) return;
    $requested = navar_requested_language($_GET['navar_lang']);
    if (!$requested) return;
    $current_id = absint(get_queried_object_id());
    $targets = navar_translation_targets($current_id);
    if (empty($targets[$requested])) return;
    $target_id = absint($targets[$requested]);
    if (!$target_id || $target_id === $current_id) return;
    $destination = get_permalink($target_id);
    if (!$destination) return;
    wp_safe_redirect($destination, 301, 'Navar Translation Link Fix');
    exit;
}, 0);

/* Rewrite only anchors that actually contain navar_lang; no text matching or competing click handlers. */
add_action('wp_footer', function () {
    if (!is_singular('post')) return;
    $targets = navar_translation_targets(get_queried_object_id());
    $links = array();
    foreach ($targets as $language => $target_id) {
        $country = navar_country_value($language);
        if ($country) $links[$country] = get_permalink($target_id);
    }
    ?>
<script id="navar-translation-link-fix-v200">
(() => {
  const links = <?php echo wp_json_encode($links, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE); ?>;
  for (const anchor of document.querySelectorAll('a[href]')) {
    try {
      const url = new URL(anchor.href, location.href);
      const requested = (url.searchParams.get('navar_lang') || '').toLowerCase();
      if (links[requested]) anchor.href = links[requested];
    } catch (_) {}
  }
})();
</script>
    <?php
}, 999);
