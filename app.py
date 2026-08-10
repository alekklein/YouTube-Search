To achieve this, we need to implement **pagination**. Instead of doing a single API request and just returning whatever is left after filtering, the script needs to keep fetching new "pages" of results from YouTube until the desired number of filtered videos is reached.

**Important Note on API Quota:** I have added a safety limit (`MAX_PAGES = 10`) to the loop. Since YouTube charges API quota for every search, if you search for something that has very few 18+ videos, the script could theoretically search thousands of pages and drain your daily API limit in seconds. This limit ensures it checks a maximum of 500 videos (10 pages of 50) before giving up and returning what it found.

Here is the fully updated code with the pagination logic built into the search function:

```python
import streamlit as st
from googleapiclient.discovery import build
from datetime import date
import isodate

st.set_page_config(
    page_title="YouTube Advanced Search",
    page_icon="📺",
    layout="wide"
)

st.title("📺 YouTube Advanced Search")
st.caption("Filter YouTube by type, age-restriction, and advanced metrics.")

# ---------------------- SIDEBAR ----------------------
with st.sidebar:
    st.header("Search Configuration")

    # Try to load API key from secrets first
    api_key = None
    try:
        api_key = st.secrets["YOUTUBE_API_KEY"]
        st.success("API key loaded from secrets", icon="🔒")
    except Exception:
        api_key = st.text_input(
            "YouTube Data API Key",
            type="password",
            help="Add it in Streamlit Secrets as YOUTUBE_API_KEY for automatic loading."
        )

    st.divider()

    # --- Core Content Settings ---
    st.subheader("Content Settings")
    
    # 1. Dropdown for Video Type
    mode = st.selectbox(
        "Video Type",
        options=["Any Videos", "Past Livestreams (completed)"],
        index=0
    )
    
    # 2. Universal 18+ Checkbox directly underneath
    only_age_restricted = st.checkbox(
        "Only 18+ videos", 
        value=True, 
        help="Check this to exclusively show age-restricted content."
    )

    st.divider()

    # --- Search Query ---
    st.subheader("Search terms")
    query = st.text_input("Keywords", placeholder="e.g. body painting", label_visibility="collapsed")

    # --- Advanced Filters (Collapsible for cleaner UI) ---
    with st.expander("Advanced Filters", expanded=False):
        st.markdown("**Date Range**")
        col1, col2 = st.columns(2)
        with col1:
            date_after = st.date_input("After", value=None)
        with col2:
            date_before = st.date_input("Before", value=None)

        st.markdown("**View Count**")
        min_views = st.number_input("Min views", min_value=0, value=0, step=100)
        max_views = st.number_input("Max views (0 = any)", min_value=0, value=0, step=1000)

        st.markdown("**Duration (seconds)**")
        min_duration = st.number_input("Min duration", min_value=0, value=0, step=30)
        max_duration = st.number_input("Max duration (0 = any)", min_value=0, value=0, step=30)

    # --- Sorting & Output Settings ---
    st.subheader("Results Format")
    order = st.selectbox(
        "Sort by",
        options=["viewCount", "date", "relevance"],
        format_func=lambda x: {"viewCount": "View Count", "date": "Date", "relevance": "Relevance"}[x]
    )

    max_results = st.slider("Target results to fetch", 5, 50, 30, help="The script will keep searching until it hits this number of filtered results.")

    st.write("") # Spacer
    search_button = st.button("Search", type="primary", use_container_width=True)


# ---------------------- SEARCH FUNCTION ----------------------
def search_videos(api_key, query, mode, published_after, published_before,
                  order, target_results, min_views, max_views,
                  min_duration, max_duration, only_age_restricted):

    youtube = build("youtube", "v3", developerKey=api_key)

    search_params = {
        "q": query,
        "part": "id,snippet",
        "type": "video",
        "order": order,
        "publishedAfter": published_after,
        "publishedBefore": published_before,
        "maxResults": 50  # Always fetch chunks of 50 to minimize API calls
    }

    if mode == "Past Livestreams (completed)":
        search_params["eventType"] = "completed"

    results = []
    next_page_token = None
    
    # SAFETY LIMIT: Prevent infinite loops and API quota exhaustion.
    # 10 pages * 50 results = 500 videos searched maximum per query.
    MAX_PAGES = 10 
    pages_searched = 0

    # Keep looping until we hit our target OR run out of pages OR hit our safety limit
    while len(results) < target_results and pages_searched < MAX_PAGES:
        if next_page_token:
            search_params["pageToken"] = next_page_token
            
        request = youtube.search().list(**search_params)
        response = request.execute()
        pages_searched += 1

        video_ids = [item["id"]["videoId"] for item in response.get("items", [])]

        if not video_ids:
            break  # No more videos found at all

        videos_response = youtube.videos().list(
            part="snippet,statistics,contentDetails,liveStreamingDetails",
            id=",".join(video_ids)
        ).execute()

        for video in videos_response.get("items", []):
            # Stop adding if we've reached our exact target
            if len(results) >= target_results:
                break

            stats = video.get("statistics", {})
            view_count = int(stats.get("viewCount", 0))

            if view_count < min_views:
                continue
            if max_views > 0 and view_count > max_views:
                continue

            duration = isodate.parse_duration(video["contentDetails"]["duration"])
            duration_seconds = int(duration.total_seconds())

            if duration_seconds < min_duration:
                continue
            if max_duration > 0 and duration_seconds > max_duration:
                continue

            content_rating = video["contentDetails"].get("contentRating", {})
            is_age_restricted = content_rating.get("ytRating") == "ytAgeRestricted"

            if only_age_restricted and not is_age_restricted:
                continue

            live_details = video.get("liveStreamingDetails", {})
            was_live = "actualStartTime" in live_details

            results.append({
                "title": video["snippet"]["title"],
                "video_id": video["id"],
                "url": f"https://www.youtube.com/watch?v={video['id']}",
                "channel": video["snippet"]["channelTitle"],
                "published": video["snippet"]["publishedAt"][:10],
                "views": view_count,
                "duration": str(duration),
                "duration_seconds": duration_seconds,
                "was_live": was_live,
                "age_restricted": is_age_restricted,
                "thumbnail": video["snippet"]["thumbnails"]["medium"]["url"]
            })

        # Get the token for the next page. If it doesn't exist, we've hit the end of YouTube's results.
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return results, pages_searched


# ---------------------- MAIN AREA ----------------------
if search_button:
    if not api_key:
        st.error("Please provide a YouTube Data API key (either in Secrets or in the sidebar).")
    elif not query.strip():
        st.warning("Please enter a search term.")
    else:
        published_after = f"{date_after}T00:00:00Z" if date_after else None
        published_before = f"{date_before}T23:59:59Z" if date_before else None

        with st.spinner("Searching YouTube (this may take a moment while fetching multiple pages)..."):
            try:
                results, pages_searched = search_videos(
                    api_key=api_key,
                    query=query,
                    mode=mode,
                    published_after=published_after,
                    published_before=published_before,
                    order=order,
                    target_results=max_results,
                    min_views=min_views,
                    max_views=max_views,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    only_age_restricted=only_age_restricted
                )

                if not results:
                    st.info(f"No videos matched your filters. (Searched through {pages_searched * 50} total videos)")
                else:
                    if len(results) < max_results:
                        st.warning(f"Only found {len(results)} videos after searching {pages_searched * 50} items. We stopped searching to save your API quota.")
                    else:
                        st.success(f"Found {len(results)} videos!")

                    for video in results:
                        with st.container():
                            col_img, col_info = st.columns([1, 4])

                            with col_img:
                                st.image(video["thumbnail"], use_container_width=True)

                            with col_info:
                                st.markdown(f"### [{video['title']}]({video['url']})")
                                
                                # Render the visual chips
                                chips_html = ""
                                if video["was_live"]:
                                    chips_html += "<span style='background-color: #ff4b4b; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.8em; font-weight: bold; margin-right: 8px;'>🔴 PAST LIVE</span>"
                                if video["age_restricted"]:
                                    chips_html += "<span style='background-color: #ff9f36; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.8em; font-weight: bold; margin-right: 8px;'>🔞 18+</span>"
                                
                                if chips_html:
                                    st.markdown(chips_html, unsafe_allow_html=True)
                                
                                st.write("") # Spacer

                                st.write(f"**Channel:** {video['channel']}")
                                st.write(
                                    f"**Views:** {video['views']:,}  |  "
                                    f"**Published:** {video['published']}  |  "
                                    f"**Duration:** {video['duration']}"
                                )

                        st.divider()

            except Exception as e:
                st.error(f"An error occurred: {str(e)}")
                st.info("Ensure your API key is valid and YouTube Data API v3 is enabled in your Google Cloud Console.")

else:
    st.info("← Enter a keyword, configure your filters in the sidebar, and click **Search**.")

    with st.expander("How it works & Limitations"):
        st.markdown("""
        - **Deep Searching:** To ensure you get the exact amount of results requested, this tool uses API pagination to automatically flip through YouTube's search pages in the background until the target number of filtered results is reached.
        - **Safety Limits:** To protect your API quota, the script is hard-capped to read a maximum of 10 pages (500 videos) per search.
        - **Age Restriction limitations:** The YouTube Data API cannot directly filter for *only* age-restricted videos. This tool systematically checks the underlying content rating of videos to apply the filter.
        - **Deploying this app:** For a smooth user experience, deploy on Streamlit Community Cloud and add your API key in **Secrets**:
          ```toml
          YOUTUBE_API_KEY = "your_key_here"
          ```
        """)

```
