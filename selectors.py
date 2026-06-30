# VK's frontend markup changes often and isn't documented, so these selectors
# are best-effort starting points, not guarantees. If a step in vk_mirror.py
# fails, open the relevant VK page in a real browser, inspect the element
# with devtools, and update the matching entry here -- the rest of the
# script does not need to change.

GROUP = {
    # Container that wraps a single wall post in the group feed.
    "post_card": '[data-testid="wall_post"], .post, [class*="WallPost__"]',
    "post_id": "[data-post-id]",
    # Text node whose rendered line breaks define the paragraphs to replicate.
    "post_text": '.wall_post_text, [class*="WallPostText"], [data-testid="post-text"]',
    "post_image": 'img.page_post_thumb_img, img[class*="PhotoPreview"], a.page_post_thumb img',
    "feed_load_more_trigger": '.ui_loader_initial, [class*="WallFeed__loadMore"]',
}

CHANNEL = {
    # Opens the post composer in the channel.
    "open_composer_button": '#mini_button, [data-testid="wall_post_open"], .post_field_placeholder',
    "composer_text_area": '#post_box_act, [data-testid="postbox-textarea"], div[contenteditable="true"][role="textbox"]',
    "attach_photo_button": "#postbox_actions_attach, [data-testid='attach-photo-button']",
    "file_input": 'input[type="file"]',
    "submit_post_button": '#post_btn, [data-testid="wall_post_submit"], button[class*="PostButton"]',
    "post_success_toast": '.snackbar, [class*="Snackbar"]',
}

LOGIN = {
    # Presence of this element means we are logged in.
    "logged_in_marker": '#side_bar, [data-testid="navigation"], #l_pr',
}
