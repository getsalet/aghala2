<?php
/**
 * Plugin Name: Navar Multilingual Permalinks
 * Plugin URI:  https://navar-abyari.ir/
 * Description: Five-language routing: Persian at root, Arabic under /iraq/, Tajik under /tj/, English under /en/, and Russian under /ru/.
 * Version:     3.1.0
 * Author:      Navar Abyari
 * License:     GPL-2.0-or-later
 * Text Domain: navar-multilingual-permalinks
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

final class Navar_Multilingual_Permalinks {
    const VERSION = '3.1.0';

    public static function routes() {
        $routes = array(
            'iraq' => array(
                'base'           => 'iraq',
                'legacy_bases'   => array( 'iq' ),
                'category_slugs' => array( 'iraq' ),
                'html_lang'      => 'ar-IQ',
                'locale'         => 'ar_IQ',
                'dir'            => 'rtl',
            ),
            'tj' => array(
                'base'           => 'tj',
                'category_slugs' => array( 'tj', 'tajik', 'tajikistan' ),
                'html_lang'      => 'tg-TJ',
                'locale'         => 'tg_TJ',
                'dir'            => 'ltr',
            ),
            'en' => array(
                'base'           => 'en',
                'category_slugs' => array( 'en', 'english' ),
                'html_lang'      => 'en-US',
                'locale'         => 'en_US',
                'dir'            => 'ltr',
            ),
            'ru' => array(
                'base'           => 'ru',
                'category_slugs' => array( 'ru', 'russian' ),
                'html_lang'      => 'ru-RU',
                'locale'         => 'ru_RU',
                'dir'            => 'ltr',
            ),
        );

        return apply_filters( 'navar_multilingual_routes', $routes );
    }

    public static function init() {
        add_action( 'init', array( __CLASS__, 'add_rewrite_rules' ) );
        add_filter( 'query_vars', array( __CLASS__, 'add_query_vars' ) );
        add_filter( 'locale', array( __CLASS__, 'filter_frontend_locale' ), 20 );
        add_filter( 'post_link', array( __CLASS__, 'filter_post_permalink' ), 10, 3 );
        add_action( 'template_redirect', array( __CLASS__, 'redirect_to_canonical_permalink' ), 1 );
        add_filter( 'language_attributes', array( __CLASS__, 'filter_language_attributes' ), 20, 2 );
        add_filter( 'body_class', array( __CLASS__, 'add_language_body_classes' ) );
        add_action( 'wp_head', array( __CLASS__, 'output_hreflang_links' ), 2 );
        add_action( 'admin_notices', array( __CLASS__, 'category_notice' ) );
    }

    public static function add_query_vars( $vars ) {
        $vars[] = 'navar_lang';
        return $vars;
    }

    public static function filter_frontend_locale( $locale ) {
        if ( is_admin() ) {
            return $locale;
        }
        $request_uri = isset( $_SERVER['REQUEST_URI'] ) ? wp_unslash( $_SERVER['REQUEST_URI'] ) : '';
        $path = trim( (string) wp_parse_url( $request_uri, PHP_URL_PATH ), '/' );
        $first = sanitize_key( (string) strtok( $path, '/' ) );
        foreach ( self::routes() as $route ) {
            $bases = array_merge( array( $route['base'] ), (array) ( $route['legacy_bases'] ?? array() ) );
            if ( in_array( $first, $bases, true ) ) {
                return $route['locale'];
            }
        }
        return $locale;
    }

    /** Resolve /iraq/post-slug/ and /tj/post-slug/. */
    public static function add_rewrite_rules() {
        foreach ( self::routes() as $key => $route ) {
            $base = preg_quote( sanitize_title( $route['base'] ), '#' );
            add_rewrite_rule(
                '^' . $base . '/([^/]+)/?$',
                'index.php?post_type=post&name=$matches[1]&navar_lang=' . sanitize_key( $key ),
                'top'
            );
            foreach ( (array) ( $route['legacy_bases'] ?? array() ) as $legacy_base ) {
                $legacy_base = preg_quote( sanitize_title( $legacy_base ), '#' );
                add_rewrite_rule(
                    '^' . $legacy_base . '/([^/]+)/?$',
                    'index.php?post_type=post&name=$matches[1]&navar_lang=' . sanitize_key( $key ),
                    'top'
                );
            }
        }
    }

    public static function route_for_post( $post ) {
        if ( ! $post instanceof WP_Post || 'post' !== $post->post_type ) {
            return null;
        }

        $slugs = wp_get_post_terms( $post->ID, 'category', array( 'fields' => 'slugs' ) );
        if ( is_wp_error( $slugs ) ) {
            return null;
        }

        foreach ( self::routes() as $key => $route ) {
            if ( array_intersect( (array) $route['category_slugs'], $slugs ) ) {
                $route['key'] = $key;
                return $route;
            }
        }

        return null;
    }

    public static function current_language_key() {
        if ( is_singular( 'post' ) ) {
            $route = self::route_for_post( get_queried_object() );
            return $route ? $route['key'] : 'fa';
        }

        if ( is_category() ) {
            $term = get_queried_object();
            if ( $term instanceof WP_Term ) {
                foreach ( self::routes() as $key => $route ) {
                    if ( in_array( $term->slug, (array) $route['category_slugs'], true ) ) {
                        return $key;
                    }
                }
            }
        }

        $query_language = sanitize_key( (string) get_query_var( 'navar_lang' ) );
        return isset( self::routes()[ $query_language ] ) ? $query_language : 'fa';
    }

    /** Prefix only Iraq and Tajik posts. Persian remains at the root. */
    public static function filter_post_permalink( $permalink, $post, $leavename ) {
        $route = self::route_for_post( $post );
        if ( ! $route ) {
            return $permalink;
        }

        $slug = $leavename ? '%postname%' : $post->post_name;
        if ( '' === $slug ) {
            return $permalink;
        }

        $path = user_trailingslashit( trim( $route['base'], '/' ) . '/' . $slug, 'single' );
        return home_url( '/' . ltrim( $path, '/' ) );
    }

    /** Redirect old and incorrectly prefixed URLs to the one canonical URL. */
    public static function redirect_to_canonical_permalink() {
        if ( is_admin() || is_preview() || is_feed() || is_trackback() || ! is_singular( 'post' ) ) {
            return;
        }

        $post = get_queried_object();
        if ( ! $post instanceof WP_Post ) {
            return;
        }

        $request_uri  = isset( $_SERVER['REQUEST_URI'] ) ? wp_unslash( $_SERVER['REQUEST_URI'] ) : '';
        $current_path = wp_parse_url( $request_uri, PHP_URL_PATH );
        $target       = get_permalink( $post );
        $target_path  = wp_parse_url( $target, PHP_URL_PATH );

        if ( ! is_string( $current_path ) || ! is_string( $target_path ) ) {
            return;
        }

        $current_path = untrailingslashit( rawurldecode( $current_path ) );
        $target_path  = untrailingslashit( rawurldecode( $target_path ) );
        $relative     = trim( $current_path, '/' );
        $home_path    = trim( (string) wp_parse_url( home_url( '/' ), PHP_URL_PATH ), '/' );

        if ( $home_path && 0 === strpos( $relative, $home_path . '/' ) ) {
            $relative = substr( $relative, strlen( $home_path ) + 1 );
        }

        $first_segment = strtok( $relative, '/' );
        $known_bases   = wp_list_pluck( self::routes(), 'base' );
        foreach ( self::routes() as $route ) {
            $known_bases = array_merge( $known_bases, (array) ( $route['legacy_bases'] ?? array() ) );
        }
        $has_prefix    = in_array( $first_segment, $known_bases, true );
        $has_route     = (bool) self::route_for_post( $post );

        if ( $current_path !== $target_path && ( $has_route || $has_prefix ) ) {
            wp_safe_redirect( $target, 301, 'Navar Multilingual Permalinks' );
            exit;
        }
    }

    public static function filter_language_attributes( $output, $doctype ) {
        $key = self::current_language_key();
        if ( 'fa' === $key ) {
            return $output;
        }

        $route = self::routes()[ $key ];
        $output = preg_replace( '/lang=("|\')[^"\']*("|\')/i', 'lang="' . esc_attr( $route['html_lang'] ) . '"', $output );
        $output = preg_replace( '/dir=("|\')[^"\']*("|\')/i', 'dir="' . esc_attr( $route['dir'] ) . '"', $output );

        if ( false === stripos( $output, 'lang=' ) ) {
            $output .= ' lang="' . esc_attr( $route['html_lang'] ) . '"';
        }
        if ( false === stripos( $output, 'dir=' ) ) {
            $output .= ' dir="' . esc_attr( $route['dir'] ) . '"';
        }

        return trim( $output );
    }

    public static function add_language_body_classes( $classes ) {
        $key = self::current_language_key();
        $classes[] = 'navar-lang-' . sanitize_html_class( $key );
        $route = self::routes()[ $key ] ?? null;
        $classes[] = 'navar-dir-' . ( $route ? $route['dir'] : 'rtl' );
        return array_unique( $classes );
    }

    /** Connect each translated post to its Persian source and sibling translations. */
    public static function output_hreflang_links() {
        if ( ! is_singular( 'post' ) ) {
            return;
        }
        $post_id   = get_queried_object_id();
        $source_id = absint( get_post_meta( $post_id, '_navar_translation_source_id', true ) );
        if ( ! $source_id ) {
            $source_id = $post_id;
        }
        $links = array();
        $source_url = get_permalink( $source_id );
        if ( $source_url ) {
            $links['fa-IR'] = $source_url;
            $links['x-default'] = $source_url;
        }
        $translated = get_posts( array(
            'post_type'              => 'post',
            'post_status'            => 'publish',
            'posts_per_page'         => -1,
            'fields'                 => 'ids',
            'no_found_rows'          => true,
            'update_post_meta_cache' => true,
            'meta_key'               => '_navar_translation_source_id',
            'meta_value'             => (string) $source_id,
        ) );
        foreach ( $translated as $translated_id ) {
            $language = (string) get_post_meta( $translated_id, '_navar_translation_language', true );
            if ( in_array( $language, array( 'ar-IQ', 'tg-TJ', 'en-US', 'ru-RU' ), true ) ) {
                $links[ $language ] = get_permalink( $translated_id );
            }
        }
        foreach ( array_filter( $links ) as $language => $url ) {
            printf(
                "<link rel=\"alternate\" hreflang=\"%s\" href=\"%s\" />\n",
                esc_attr( $language ),
                esc_url( $url )
            );
        }
    }

    public static function category_notice() {
        if ( ! current_user_can( 'manage_options' ) ) {
            return;
        }

        $missing = array();
        foreach ( self::routes() as $key => $route ) {
            $found = false;
            foreach ( (array) $route['category_slugs'] as $slug ) {
                if ( get_category_by_slug( $slug ) ) {
                    $found = true;
                    break;
                }
            }
            if ( ! $found ) {
                $missing[] = $key . ': ' . implode( ' / ', (array) $route['category_slugs'] );
            }
        }

        if ( ! $missing ) {
            return;
        }

        echo '<div class="notice notice-warning"><p>';
        echo esc_html( 'Navar Multilingual Permalinks: category not found — ' . implode( ' | ', $missing ) );
        echo '</p></div>';
    }

    public static function activate() {
        self::add_rewrite_rules();
        flush_rewrite_rules();
    }

    public static function deactivate() {
        flush_rewrite_rules();
    }
}

/** Public helper for the child theme and custom templates. */
function navar_current_language() {
    return Navar_Multilingual_Permalinks::current_language_key();
}

Navar_Multilingual_Permalinks::init();
register_activation_hook( __FILE__, array( 'Navar_Multilingual_Permalinks', 'activate' ) );
register_deactivation_hook( __FILE__, array( 'Navar_Multilingual_Permalinks', 'deactivate' ) );
