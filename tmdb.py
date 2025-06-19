import re
import aiohttp
import asyncio
from config import TMDB_API_KEY, logger
from imdb import IMDb


POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'
PROFILE_BASE_URL = 'https://image.tmdb.org/t/p/w500'

def profile_url(path):
    return f"{PROFILE_BASE_URL}{path}" if path else None


def clean_genre_name(genre):
    return re.sub(r'[^A-Za-z0-9]', '', genre)


def extract_language(data):
    spoken_languages = data.get('spoken_languages', [])
    if spoken_languages:
        return ", ".join(lang.get('english_name', 'Unknown') for lang in spoken_languages)
    return "Unknown"

def extract_genres(data):
    genres = []
    for genre in data.get('genres', []):
        if '&' in genre['name']:
            parts = [g.strip() for g in genre['name'].split('&')]
            genres.extend(parts)
        else:
            genres.append(genre['name'])
    return genres

def extract_release_date(data):
    return data.get('release_date') or data.get('first_air_date', "")

def extract_directors(tmdb_type, data, credits):
    directors = []
    if tmdb_type == 'movie':
        for member in credits.get('crew', []):
            if member.get('job') == 'Director':
                directors.append({
                    "name": member.get('name'),
                    "profile_path": profile_url(member.get('profile_path'))
                })
    elif tmdb_type == 'tv':
        for creator in data.get('created_by', []):
            directors.append({
                "name": creator.get('name'),
                "profile_path": profile_url(creator.get('profile_path'))
            })
    return directors

def extract_stars(credits, limit=5):
    cast_list = credits.get('cast', [])
    if not cast_list:
        return []
    return [
        {
            "name": member.get('name'),
            "profile_path": profile_url(member.get('profile_path'))
        }
        for member in cast_list[:limit]
    ]

def get_poster_url(data):
    poster_path = data.get('poster_path')
    return f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

def get_backdrop_url(movie_images):
    for key in ['backdrops', 'posters']:
        if key in movie_images and movie_images[key]:
            path = movie_images[key][0].get('file_path')
            if path:
                return f"{POSTER_BASE_URL}{path}"
    return None

async def get_trailer_url(session, tmdb_type, tmdb_id):
    video_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}'
    async with session.get(video_url) as video_response:
        if video_response.status == 200:
            data = await video_response.json()
            results = data.get('results', [])
            for video in results:
                if video.get('site') == 'YouTube' and video.get('type') == 'Trailer':
                    return f"https://www.youtube.com/watch?v={video.get('key')}"
    return None

async def get_by_id(tmdb_type, tmdb_id, season=None, episode=None):
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    images_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/images?api_key={TMDB_API_KEY}&language=en-US&include_image_language=en,hi'
    credits_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/credits?api_key={TMDB_API_KEY}&language=en-US"
    try:
        async with aiohttp.ClientSession() as session:
            detail_resp, images_resp, credits_resp = await asyncio.gather(
                session.get(api_url), session.get(images_url), session.get(credits_url)
            )
            data = await detail_resp.json()
            movie_images = await images_resp.json()
            credits = await credits_resp.json()

            poster_url = get_poster_url(data)
            backdrop_url = get_backdrop_url(movie_images)
            trailer_url = await get_trailer_url(session, tmdb_type, tmdb_id)

            directors_list = extract_directors(tmdb_type, data, credits)
            stars_list = extract_stars(credits)

            directors_str = ", ".join([d["name"] for d in directors_list]) if directors_list else "Unknown"
            stars_str = ", ".join([s["name"] for s in stars_list]) if stars_list else "Unknown"

            language = extract_language(data)
            genres = extract_genres(data)
            release_date = extract_release_date(data)

            imdb_id = data.get('imdb_id')
            # For TV, fetch imdb_id if not present
            if tmdb_type == 'tv' and not imdb_id:
                imdb_id = await get_tv_imdb_id(data.get('id'))
            plot = ""
            if imdb_id:
                plot = await asyncio.to_thread(get_imdb_plot, imdb_id)
            if not plot:
                plot = data.get('overview', '')


            message = await format_tmdb_info(
                tmdb_type, season, episode, 
                directors_str, stars_str, data, plot
            )

            mongo_dict = {
                "tmdb_id": tmdb_id,
                "tmdb_type": tmdb_type,
                "title": data.get('title') or data.get('name'),
                "rating": round(float(data.get('vote_average', 0)), 1),
                "language": language,
                "genre": genres,
                "release_date": release_date,
                "story": plot or data.get('overview'),
                "directors": directors_list,
                "stars": stars_list,
                "trailer_url": trailer_url,
                "poster_url": poster_url
            }
            return {
                "message": message,
                "poster_url": poster_url,
                "backdrop_url": backdrop_url,
                "trailer_url": trailer_url,
                "mongo_dict": mongo_dict
            }

    except aiohttp.ClientError as e:
        logger.error(f"Error fetching TMDB data: {e}")
        return {"message": f"Error: {str(e)}", "poster_url": None}
    except Exception as e:
        logger.error(f"Unknown error in get_by_id: {e}")
        return {"message": f"Error: {str(e)}", "poster_url": None}
    return {"message": "Unknown error occurred.", "poster_url": None}

async def get_tv_imdb_id(tv_id):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data.get("imdb_id")

def get_imdb_plot(imdb_id):
    """Fetch plot from IMDb using IMDbPY."""
    try:
        ia = IMDb()
        movie = ia.get_movie(imdb_id.replace('tt', ''))
        plot = movie.get('plot', [''])[0]
        plot = plot.split('::')[0].strip()
        return plot
    except Exception as e:
        logger.error(f"Error fetching IMDb plot: {e}")
        return ""

async def format_tmdb_info(tmdb_type, season, episode, directors_str, stars_str, data, plot):
    # Title and year/season/episode
    if tmdb_type == 'movie':
        title = data.get('title', '')
        year = data.get('release_date', '')[:4] if data.get('release_date') else ''
        header = f"<b>{title} ({year})</b> is now available!"
    elif tmdb_type == 'tv':
        title = data.get('name', '')
        year = data.get('first_air_date', '')[:4] if data.get('first_air_date') else ''
        # Only add year for season 1 or (season 1 and episode 1)
        if season and episode:
            if season == 1 and episode == 1:
                header = f"<b>{title} ({year}) S{season:02d}E{episode:02d}</b> is now available!"
            else:
                header = f"<b>{title} S{season:02d}E{episode:02d}</b> is now available!"
        elif season:
            if season == 1:
                header = f"<b>{title} ({year}) Season {season}</b> is now available!"
            else:
                header = f"<b>{title} Season {season}</b> is now available!"
        else:
            header = f"<b>{title}</b> is now available!"
    else:
        header = "Now Available!"

    # Genres: remove spaces and special characters -, :, &, then add "#"
    genres = [g['name'] for g in data.get('genres', [])]
    genres_clean = ['#' + ''.join(c for c in g if c.isalnum()) for g in genres]
    genres_str = ' '.join(genres_clean)

    # If season or episode > 1, only show header and genres
    if (season and season > 1) or (episode and episode > 1):
        message = f"{header}\n\n{genres_str}"
        return message.strip()

    # Format message
    message = f"{header}\n\n"
    message += f"{plot}\n\n" if plot else ""
    message += f"<b>Stars:</b> {stars_str}\n\n" if stars_str else ""
    message += f"<b>Directors:</b> {directors_str}\n\n" if directors_str else ""
    message += f"{genres_str}"

    return message.strip()

def truncate_overview(overview):
    MAX_OVERVIEW_LENGTH = 600
    return overview[:MAX_OVERVIEW_LENGTH] + "..." if len(overview) > MAX_OVERVIEW_LENGTH else overview

def format_duration(duration):
    try:
        mins = int(duration)
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins:02d}min" if hours else f"{mins}min"
    except Exception:
        return str(duration) if duration else ""

async def get_movie_by_name(movie_name, release_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={movie_name}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if release_year:
                        results = [
                            result for result in results
                            if 'release_date' in result and result['release_date'] and result['release_date'][:4] == str(release_year)
                        ]
                    if results:
                        result = results[0]
                        return {
                            "id": result['id'],
                            "media_type": "movie"
                        }
        return None
    except Exception as e:
        logger.error(f"Error fetching TMDb movie by name: {e}")
        return

async def get_tv_by_name(tv_name, first_air_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={tv_name}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if first_air_year:
                        results = [
                            result for result in results
                            if 'first_air_date' in result and result['first_air_date'] and result['first_air_date'][:4] == str(first_air_year)
                        ]
                    if results:
                        result = results[0]
                        return {
                            "id": result['id'],
                            "media_type": "tv"
                        }
        return None
    except Exception as e:
        logger.error(f"Error fetching TMDb TV by name: {e}")
        return