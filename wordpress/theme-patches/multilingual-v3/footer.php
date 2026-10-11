<?php


if ( ! defined( 'ABSPATH' ) ) {
	exit; 
}
?>
	</div>
</div>

<?php

do_action( 'generate_before_footer' );
?>

<div class="footer-divider">
	<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 320">
		<path fill="#14202b" fill-opacity="1" d="M0,192L80,202.7C160,213,320,235,480,208C640,181,800,107,960,80C1120,53,1280,75,1360,85.3L1440,96L1440,320L1360,320C1280,320,1120,320,960,320C800,320,640,320,480,320C320,320,160,320,80,320L0,320Z"></path>
	</svg>


</div>

<div <?php generate_do_attr( 'footer' ); ?>>
	<?php

	do_action( 'generate_before_footer_content' );

	do_action( 'generate_footer' );

	do_action( 'generate_after_footer_content' );
	?>
</div>

<?php

do_action( 'generate_after_footer' );

wp_footer();
?>


<?php
$navar_language_key = function_exists( 'navar_current_language' ) ? navar_current_language() : 'fa';
$navar_language_map = array( 'fa' => 'fa-IR', 'iraq' => 'ar-IQ', 'tj' => 'tg-TJ', 'en' => 'en-US', 'ru' => 'ru-RU' );
$navar_page_language = isset( $navar_language_map[ $navar_language_key ] ) ? $navar_language_map[ $navar_language_key ] : 'fa-IR';
?>
<script>
// ======== Session و Pages Visited ========
let sessionId = localStorage.getItem('session_id');
if (!sessionId) {
    sessionId = 'sess_' + Math.random().toString(36).substr(2, 9);
    localStorage.setItem('session_id', sessionId);
}

let pagesVisited = Number(localStorage.getItem('pages_visited') || 0);
pagesVisited += 1; // این صفحه هم بازدید شده
localStorage.setItem('pages_visited', pagesVisited);

const userLanguage = navigator.language || 'unknown';
const pageLanguage = <?php echo wp_json_encode( $navar_page_language ); ?>;

// ======== User Engagement Time ========
let lastTimestamp = Date.now();
let engagementTime = 0;

function accumulateTime() {
    const now = Date.now();
    const diff = now - lastTimestamp;
    engagementTime += diff;
    lastTimestamp = now;
}

setInterval(() => {
    accumulateTime();
    gtag('event', 'user_engagement', {
        engagement_time_msec: engagementTime,
        session_id: sessionId,
        language: userLanguage,
        language_override: pageLanguage,
        pages_visited: pagesVisited
    });
}, 10000);

document.addEventListener("visibilitychange", () => {
    accumulateTime();
    lastTimestamp = Date.now();
});

window.addEventListener("beforeunload", () => {
    accumulateTime();
    gtag('event', 'user_engagement', {
        engagement_time_msec: engagementTime,
        session_id: sessionId,
        language: userLanguage,
        language_override: pageLanguage,
        pages_visited: pagesVisited
    });
});

// ======== Scroll Depth ========
let scrollMilestones = [25, 50, 75, 100];
window.addEventListener('scroll', () => {
    const scrollTop = window.scrollY;
    const docHeight = document.documentElement.scrollHeight - window.innerHeight;
    const percent = Math.round((scrollTop / docHeight) * 100);

    scrollMilestones = scrollMilestones.filter(m => {
        if (percent >= m) {
            gtag('event', 'scroll_depth', {
                scroll_percent: m,
                event_label: m + '%',
                event_category: 'Scroll',
                session_id: sessionId,
                language: userLanguage,
                language_override: pageLanguage,
                pages_visited: pagesVisited
            });
            return false;
        }
        return true;
    });
});

// ======== Link Click Tracking ========
document.addEventListener('click', (e) => {
    const link = e.target.closest('a');
    if (!link || !link.href) return;

    const currentDomain = window.location.hostname;
    const linkDomain = new URL(link.href).hostname;
    const isExternal = currentDomain !== linkDomain;
    const isAnchor = link.hash && link.pathname === window.location.pathname;

    if (isExternal) {
        gtag('event', 'link_click', {
            event_category: 'External Link',
            event_label: link.href,
            link_domain: linkDomain,
            link_type: 'external',
            session_id: sessionId,
            language: userLanguage,
            language_override: pageLanguage,
            pages_visited: pagesVisited
        });
    } else if (isAnchor) {
        gtag('event', 'link_click', {
            event_category: 'Anchor Link',
            event_label: link.href,
            link_domain: linkDomain,
            link_type: 'anchor',
            session_id: sessionId,
            language: userLanguage,
            language_override: pageLanguage,
            pages_visited: pagesVisited
        });
    } else {
        gtag('event', 'link_click', {
            event_category: 'Internal Link',
            event_label: link.href,
            link_domain: linkDomain,
            link_type: 'internal',
            session_id: sessionId,
            language: userLanguage,
            language_override: pageLanguage,
            pages_visited: pagesVisited,
            transport_type: 'beacon'
        });
    }
});
</script>



</body>
</html>
