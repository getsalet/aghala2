<?php
/**
 * Plugin Name: Navar Sidebar Language Filter
 * Description: Prevents sidebar and secondary post lists from mixing Persian, Arabic and Tajik articles.
 * Version: 2.1.0
 * Author: Navar Abyari
 */
if (!defined('ABSPATH')) exit;

function navar_sidebar_lang_normalize($value) {
    $value = strtolower(sanitize_key((string) $value));
    $map = array(
        'iran' => 'fa-IR', 'fa' => 'fa-IR', 'fa-ir' => 'fa-IR', 'persian' => 'fa-IR',
        'iraq' => 'ar-IQ', 'iq' => 'ar-IQ', 'ar' => 'ar-IQ', 'ar-iq' => 'ar-IQ', 'arabic' => 'ar-IQ',
        'tajikistan' => 'tg-TJ', 'tj' => 'tg-TJ', 'tg' => 'tg-TJ', 'tg-tj' => 'tg-TJ', 'tajik' => 'tg-TJ',
        'english' => 'en-US', 'en' => 'en-US', 'en-us' => 'en-US',
        'russia' => 'ru-RU', 'russian' => 'ru-RU', 'ru' => 'ru-RU', 'ru-ru' => 'ru-RU',
    );
    return isset($map[$value]) ? $map[$value] : '';
}

function navar_sidebar_current_language() {
    if (isset($_GET['navar_lang'])) {
        $requested = navar_sidebar_lang_normalize(wp_unslash($_GET['navar_lang']));
        if ($requested) return $requested;
    }

    $post_id = absint(get_queried_object_id());
    if ($post_id) {
        $stored = (string) get_post_meta($post_id, '_navar_translation_language', true);
        if (in_array($stored, array('ar-IQ', 'tg-TJ', 'en-US', 'ru-RU'), true)) return $stored;
        /* Deterministic fallback for the generated translation ID range. */
        if ($post_id >= 502015 && $post_id <= 502320) {
            return (($post_id - 502015) % 2 === 0) ? 'ar-IQ' : 'tg-TJ';
        }
    }

    $path = isset($_SERVER['REQUEST_URI']) ? wp_parse_url(wp_unslash($_SERVER['REQUEST_URI']), PHP_URL_PATH) : '';
    if (is_string($path)) {
        if (preg_match('#^/(?:iraq|iq)(?:/|$)#i', $path)) return 'ar-IQ';
        if (preg_match('#^/tj(?:/|$)#i', $path)) return 'tg-TJ';
        if (preg_match('#^/en(?:/|$)#i', $path)) return 'en-US';
        if (preg_match('#^/ru(?:/|$)#i', $path)) return 'ru-RU';
    }
    return 'fa-IR';
}

function navar_sidebar_language_clause($language) {
    if (in_array($language, array('ar-IQ', 'tg-TJ', 'en-US', 'ru-RU'), true)) {
        return array(
            'key' => '_navar_translation_language',
            'value' => $language,
            'compare' => '=',
        );
    }
    return array(
        'relation' => 'OR',
        array('key' => '_navar_translation_language', 'compare' => 'NOT EXISTS'),
        array('key' => '_navar_translation_language', 'value' => '', 'compare' => '='),
    );
}

function navar_sidebar_apply_language_filter($query) {
    if (is_admin() || !($query instanceof WP_Query) || $query->is_main_query()) return;
    if ($query->get('navar_allow_mixed_languages')) return;

    $post_type = $query->get('post_type');
    if ($post_type && $post_type !== 'post' && $post_type !== 'any' && $post_type !== array('post')) return;

    $language = navar_sidebar_current_language();
    $language_clause = navar_sidebar_language_clause($language);
    $existing = $query->get('meta_query');

    if (is_array($existing) && !empty($existing)) {
        $query->set('meta_query', array('relation' => 'AND', $existing, $language_clause));
    } else {
        $query->set('meta_query', array($language_clause));
    }
}
add_action('pre_get_posts', 'navar_sidebar_apply_language_filter', 999);

/* Standard Recent Posts widget: reinforce the same rule even when a theme alters its query args. */
add_filter('widget_posts_args', function ($args) {
    $language_clause = navar_sidebar_language_clause(navar_sidebar_current_language());
    $existing = isset($args['meta_query']) && is_array($args['meta_query']) ? $args['meta_query'] : array();
    $args['meta_query'] = $existing ? array('relation' => 'AND', $existing, $language_clause) : array($language_clause);
    $args['suppress_filters'] = false;
    return $args;
}, 999);
