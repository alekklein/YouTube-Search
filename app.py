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
st.caption("Past Livestreams + Age-restricted filter + Duration filter")

# ---------------------- SIDEBAR ----------------------
with st.sidebar:
    st.header("Settings")

    # Try to load API key from secrets first
    api_key = None
    try:
        api_key = st.secrets["YOUTUBE_API_KEY"]
        st.success("API key loaded from secrets", icon="🔒")
    except Exception:
        api_key = st.text_input(
            "YouTube Data API Key",
            type="password",
            help="Or add it in Streamlit Secrets as YOUTUBE_API_KEY"
        )

    st.divider()

    mode = st.radio(
        "Search Mode",
        options=["Past Livestreams (completed)", "Any Videos + Age-restricted filter"],
        index=0
    )

    query = st.text_input("Search terms", placeholder="e.g. body painting")

    st.subheader("Date range")
    col1, col2 = st.columns(2)
    with col1:
        date_after = st.date_input("Published after", value=None)
    with col2:
        date_before = st.date_input("Published before", value=None)

    st.subheader("View count")
    min_views = st.number_input("Minimum views", min_value=0, value=0, step=100)
    max_views = st.number_input("Maximum views (0 = no limit)", min_value=0, value=0, step=1000)

    st.subheader("Duration (seconds)")
    min_duration = st.number_input("Minimum duration (seconds)", min_value=0, value=0, step=30)
    max_duration = st.number_input("Maximum duration (seconds) (0 = no limit)", min_value=0, value=0, step=30)

    order = st.selectbox(
        "Sort by",
        options=["viewCount", "date", "relevance"],
        format_func=lambda x: {"viewCount": "View Count", "date": "Date", "relevance": "Relevance"}[x]
    )

    max_results = st.slider("Max results to fetch", 5, 50, 30)

    only_age_restricted = False
    if mode == "Any Videos + Age-restricted filter":
        only_age_restricted = st.checkbox("Show only age-restricted videos", value=True)

    search_button = st.button("Search", type="primary", use_container_width=True)

# ---------------------- SEARCH FUNCTION ----------------------
def search_videos(api_key, query, mode, published_after, published_before,
                  order, max_results, min_views, max_views,
                  min_duration, max_duration, only_age_restricted):

    youtube = build("youtube", "v3", developerKey=api_key)

    search_params = {
        "q": query,
        "part": "id,snippet",
        "type": "video",
        "order": order,
        "publishedAfter": published_after,
        "publishedBefore": published_before,
        "maxResults": min(max_results, 50)
    }

    if mode == "Past Livestreams (completed)":
        search_params["eventType"] = "completed"

    request = youtube.search().list(**search_params)
    response = request.execute()

    video_ids = [item["id"]["videoId"] for item in response.get("items", [])]

    if not video_ids:
        return []

    videos_response = youtube.videos().list(
        part="snippet,statistics,contentDetails,liveStreamingDetails",
        id=",".join(video_ids)
    ).execute()

    results = []
    for video in videos_response.get("items", []):
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

    return results

# ---------------------- MAIN AREA ----------------------
if search_button:
    if not api_key:
        st.error("Please provide a YouTube Data API key (either in Secrets or in the sidebar).")
    elif not query.strip():
        st.warning("Please enter a search term.")
    else:
        published_after = f"{date_after}T00:00:00Z" if date_after else None
        published_before = f"{date_before}T23:59:59Z" if date_before else None

        with st.spinner("Searching YouTube..."):
            try:
                results = search_videos(
                    api_key=api_key,
                    query=query,
                    mode=mode,
                    published_after=published_after,
                    published_before=published_before,
                    order=order,
                    max_results=max_results,
                    min_views=min_views,
                    max_views=max_views,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    only_age_restricted=only_age_restricted
                )

                if not results:
                    st.info("No videos matched your filters.")
                else:
                    st.success(f"Found {len(results)} videos")

                    for video in results:
                        with st.container():
                            col_img, col_info = st.columns([1, 4])

                            with col_img:
                                st.image(video["thumbnail"], use_container_width=True)

                            with col_info:
                                badges = []
                                if video["was_live"]:
                                    badges.append("🔴 PAST LIVE")
                                if video["age_restricted"]:
                                    badges.append("🔞 AGE-RESTRICTED")

                                badge_text = "  ".join(badges)
                                st.markdown(f"### [{video['title']}]({video['url']}) {badge_text}")
                                st.write(f"**Channel:** {video['channel']}")
                                st.write(
                                    f"**Views:** {video['views']:,}  |  "
                                    f"**Published:** {video['published']}  |  "
                                    f"**Duration:** {video['duration']}"
                                )

                            st.divider()

            except Exception as e:
                st.error(f"Error: {str(e)}")
                st.info("Make sure your API key is valid and YouTube Data API v3 is enabled in Google Cloud Console.")

else:
    st.info("← Configure the filters in the sidebar and click **Search**.")

    with st.expander("Important notes"):
        st.markdown("""
        - The YouTube Data API cannot directly search *only* for age-restricted videos.  
          This tool fetches results and then checks the content rating.
        - Duration and view filters are applied after the API returns results.
        - For private deployment on Streamlit Community Cloud, add your API key in **Secrets**:
          ```toml
          YOUTUBE_API_KEY = "your_key_here"
          ```
        """)
