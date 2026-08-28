import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Nama role yang boleh klik "Konfirmasi Pembayaran" & "Set Tax".
# Buat role ini di server kamu (Server Settings > Roles) lalu pasang ke akun kamu sendiri.
MIDDLEMAN_ROLE_NAME = os.getenv("MIDDLEMAN_ROLE_NAME", "Middleman")

# Path gambar QRIS lokal (taruh file di folder assets/ sejajar dengan bot.py)
QRIS_IMAGE_PATH = "assets/qris.png"

# Persentase fee middleman DEFAULT (cuma dipakai sebagai nilai awal di popup
# input tax & di /mm calculator kalau kamu belum override). Tax yang beneran
# dipakai buat hitung transaksi selalu diisi manual oleh Middleman lewat
# tombol "Set Tax" di tiap tiket -- jadi mau ganti persen, gak perlu edit
# kode / restart bot lagi.
MM_FEE_PERCENT = 3

# Nama channel tempat user bisa cek ulang perhitungan tax secara manual
TAX_CHANNEL_NAME = os.getenv("TAX_CHANNEL_NAME", "midman-tax")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class TicketBot(commands.Bot):

  def __init__(self):
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    await self.tree.sync()
    print("Slash commands berhasil disinkronkan secara global!")

  async def on_guild_join(self, guild: discord.Guild):
    self.tree.copy_global_to(guild=guild)
    await self.tree.sync(guild=guild)


bot = TicketBot()

# Simpan data tiket sementara di memory, keyed by channel id.
# Kalau bot restart data ini hilang -- kalau butuh persist antar-restart,
# ganti dict ini dengan baca/tulis ke file JSON atau database.
TICKET_DATA: dict[int, dict] = {}


def is_middleman(member: discord.Member) -> bool:
  if member.guild_permissions.administrator:
    return True
  return any(role.name == MIDDLEMAN_ROLE_NAME for role in member.roles)


def hitung_fee(harga: int, persen: float = MM_FEE_PERCENT) -> tuple[int, int]:
  """Return (fee, total_transfer) dari harga barang, pakai persentase tax
  yang dikasih (default ke MM_FEE_PERCENT kalau gak dikasih, misal buat /mm)."""
  fee = round(harga * persen / 100)
  total = harga + fee
  return fee, total


# ---------- Step 1: Pilih partner + tentukan siapa Buyer (dropdown, bukan modal) ----------
class SetupView(discord.ui.View):
  """Muncul begitu tombol 'Order Middleman' diklik. User wajib pilih partner
  DAN nentuin siapa Buyer sebelum lanjut isi nama item & harga.
  Gak ada opsi skip -- tombol 'Lanjut' baru aktif kalau dua-duanya udah diisi."""

  def __init__(self, ticket_owner: discord.Member):
    super().__init__(timeout=180)
    self.ticket_owner = ticket_owner
    self.partner: discord.Member | None = None
    self.buyer_choice: str | None = None  # "owner" atau "partner"

  def _is_ticket_owner(self, interaction: discord.Interaction) -> bool:
    return interaction.user.id == self.ticket_owner.id

  @discord.ui.select(
      cls=discord.ui.UserSelect,
      placeholder="1️⃣ Pilih user lawan transaksi (buyer/seller)",
      min_values=1,
      max_values=1,
      row=0,
  )
  async def select_partner(
      self, interaction: discord.Interaction, select: discord.ui.UserSelect
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa pilih partner ini.", ephemeral=True
      )
      return

    partner = select.values[0]
    if partner.id == self.ticket_owner.id:
      await interaction.response.send_message(
          "❌ Gak bisa pilih diri sendiri sebagai partner.", ephemeral=True
      )
      return

    self.partner = partner
    await interaction.response.defer()

  @discord.ui.select(
      placeholder="2️⃣ Siapa yang jadi Buyer di transaksi ini?",
      min_values=1,
      max_values=1,
      row=1,
      options=[
          discord.SelectOption(
              label="Saya (pembuat tiket) sebagai Buyer",
              value="owner",
              emoji="🛒",
          ),
          discord.SelectOption(
              label="Lawan transaksi (partner) sebagai Buyer",
              value="partner",
              emoji="💰",
          ),
      ],
  )
  async def select_buyer(
      self, interaction: discord.Interaction, select: discord.ui.Select
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa pilih ini.", ephemeral=True
      )
      return

    self.buyer_choice = select.values[0]
    await interaction.response.defer()

  @discord.ui.button(
      label="Lanjut: Isi Nama Item & Harga",
      style=discord.ButtonStyle.green,
      row=2,
  )
  async def proceed(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa lanjut.", ephemeral=True
      )
      return

    if self.partner is None:
      await interaction.response.send_message(
          "❌ Pilih dulu lawan transaksi kamu (dropdown pertama).", ephemeral=True
      )
      return
    if self.buyer_choice is None:
      await interaction.response.send_message(
          "❌ Pilih dulu siapa yang jadi Buyer (dropdown kedua).", ephemeral=True
      )
      return

    buyer = self.ticket_owner if self.buyer_choice == "owner" else self.partner
    seller = self.partner if self.buyer_choice == "owner" else self.ticket_owner

    # Modal cuma boleh dibuka sebagai respons LANGSUNG dari sebuah interaksi
    # (tombol/select), makanya urutannya: pilih partner & buyer -> baru modal.
    await interaction.response.send_modal(
        OrderModal(self.ticket_owner, self.partner, buyer, seller)
    )
    self.stop()


# ---------- Step 2: Modal nama item + harga (setelah partner & role dipilih) ----------
class OrderModal(discord.ui.Modal, title="XypherStore - Order Middleman"):

  nama_item = discord.ui.TextInput(
      label="Nama Item / Barang",
      placeholder="Contoh: Rayman / Wings / DL",
      max_length=100,
  )
  harga_barang = discord.ui.TextInput(
      label="Harga Barang (Angka saja)",
      placeholder="Contoh: 300000",
      max_length=20,
  )

  def __init__(
      self,
      ticket_owner: discord.Member,
      partner: discord.Member,
      buyer: discord.Member,
      seller: discord.Member,
  ):
    super().__init__()
    self.ticket_owner = ticket_owner
    self.partner = partner
    self.buyer = buyer
    self.seller = seller

  async def on_submit(self, interaction: discord.Interaction):
    raw = self.harga_barang.value.replace(".", "").replace(",", "").strip()
    if not raw.isdigit():
      await interaction.response.send_message(
          "❌ Harga barang harus berupa angka.", ephemeral=True
      )
      return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await create_ticket_channel(
        interaction,
        self.ticket_owner,
        self.partner,
        self.buyer,
        self.seller,
        self.nama_item.value,
        int(raw),
    )


# ---------- Bikin channel private + kirim prompt Set Tax ----------
async def create_ticket_channel(
    interaction: discord.Interaction,
    owner: discord.Member,
    partner: discord.Member,
    buyer: discord.Member,
    seller: discord.Member,
    item_name: str,
    harga: int,
):
  guild = interaction.guild
  channel_name = f"mm-{owner.name.lower()}"

  existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
  if existing_channel:
    await interaction.followup.send(
        f"⚠️ Kamu sudah memiliki tiket aktif di {existing_channel.mention}!",
        ephemeral=True,
    )
    return

  overwrites = {
      guild.default_role: discord.PermissionOverwrite(read_messages=False),
      owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
      partner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
      guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
  }

  category = discord.utils.get(guild.categories, name="🔒 _ ACTIVE TICKETS 🎫")
  if not category:
    category = discord.utils.get(guild.categories, name="_ ACTIVE TICKETS")

  ticket_channel = await guild.create_text_channel(
      channel_name, overwrites=overwrites, category=category
  )

  # fee & total belum dihitung -- nunggu Middleman input tax dulu lewat
  # tombol "Set Tax" di bawah.
  TICKET_DATA[ticket_channel.id] = {
      "owner_id": owner.id,
      "partner_id": partner.id,
      "buyer_id": buyer.id,
      "seller_id": seller.id,
      "item_name": item_name,
      "harga": harga,
      "tax_percent": None,
      "fee": None,
      "total": None,
      "world": None,
      "growid": None,
      "payment_confirmed": False,
      "seller_detail_filled": False,
      "buyer_growid_filled": False,
      "qris_message_id": None,
      "confirm_message_id": None,
      "seller_detail_message_id": None,
      "tax_prompt_message_id": None,
  }

  intro_embed = discord.Embed(
      title="✨ Sesi Middleman XypherStore Dibuka",
      description=(
          f"Halo {owner.mention} & {partner.mention}! Terima kasih telah "
          "menggunakan jasa Middleman.\n\n"
          f"🛒 **Buyer:** {buyer.mention}\n"
          f"💰 **Seller:** {seller.mention}\n"
          f"📦 **Item:** {item_name}\n"
          f"💵 **Harga Barang:** Rp {harga:,}"
      ),
      color=discord.Color.green(),
  )
  intro_embed.set_footer(text="XypherStore Secure Transaction System")
  await ticket_channel.send(
      content=f"{owner.mention} {partner.mention}", embed=intro_embed
  )

  # Tombol tutup tiket tetap ready dari awal, gak perlu nunggu tax
  await ticket_channel.send(view=CloseTicketView())

  # ---------- Prompt buat Middleman input tax dulu ----------
  tax_prompt_embed = discord.Embed(
      title="🧮 Menunggu Middleman Set Tax",
      description=(
          f"Role `{MIDDLEMAN_ROLE_NAME}`, silakan klik tombol di bawah untuk "
          "menentukan persentase tax/fee middleman transaksi ini.\n\n"
          "Setelah tax diisi, bot akan otomatis menghitung total transfer dan "
          "menampilkan embed pembayaran + QRIS."
      ),
      color=discord.Color.orange(),
  )
  tax_prompt_msg = await ticket_channel.send(
      embed=tax_prompt_embed, view=TaxPromptView()
  )
  TICKET_DATA[ticket_channel.id]["tax_prompt_message_id"] = tax_prompt_msg.id

  await interaction.followup.send(
      f"✅ Berhasil! Cek channel transaksi kamu di: {ticket_channel.mention}",
      ephemeral=True,
  )


# ---------- Tombol "Set Tax" -- KHUSUS role Middleman ----------
class TaxPromptView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="⚙️ Set Tax (Middleman)",
      style=discord.ButtonStyle.blurple,
      custom_id="set_tax_btn",
  )
  async def set_tax(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_middleman(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{MIDDLEMAN_ROLE_NAME}` yang bisa set tax transaksi ini.",
          ephemeral=True,
      )
      return

    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if data.get("tax_percent") is not None:
      await interaction.response.send_message(
          "⚠️ Tax transaksi ini sudah pernah diisi.", ephemeral=True
      )
      return

    await interaction.response.send_modal(TaxModal())


class TaxModal(discord.ui.Modal, title="Set Tax Middleman"):

  tax_persen = discord.ui.TextInput(
      label="Persentase Tax (%)",
      placeholder="Contoh: 3 atau 2.5",
      default=str(MM_FEE_PERCENT),
      max_length=10,
  )

  async def on_submit(self, interaction: discord.Interaction):
    raw = self.tax_persen.value.strip().replace(",", ".")
    try:
      persen = float(raw)
    except ValueError:
      await interaction.response.send_message(
          "❌ Tax harus berupa angka, contoh: 3 atau 2.5", ephemeral=True
      )
      return

    if persen < 0 or persen > 100:
      await interaction.response.send_message(
          "❌ Tax harus di antara 0 - 100.", ephemeral=True
      )
      return

    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    fee, total = hitung_fee(data["harga"], persen)
    data["tax_percent"] = persen
    data["fee"] = fee
    data["total"] = total

    await interaction.response.defer()

    # Matikan tombol Set Tax biar gak dobel-klik
    prompt_msg_id = data.get("tax_prompt_message_id")
    if prompt_msg_id:
      try:
        prompt_msg = await interaction.channel.fetch_message(prompt_msg_id)
        disabled_view = TaxPromptView()
        for child in disabled_view.children:
          child.disabled = True
        await prompt_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    await kirim_payment_embed(interaction.channel, data, persen)


# ---------- Kirim embed "Pembayaran ke XypherStore Middleman" + QRIS ----------
async def kirim_payment_embed(
    channel: discord.TextChannel, data: dict, persen: float
):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  seller = guild.get_member(data["seller_id"])
  buyer_mention = buyer.mention if buyer else "Buyer"
  seller_mention = seller.mention if seller else "Seller"

  payment_embed = discord.Embed(
      title="💳 Pembayaran ke XypherStore Middleman",
      color=discord.Color.blue(),
  )
  payment_embed.add_field(
      name="🔹 Detail Transaksi",
      value=(
          f"**Item / Barang:** {data['item_name']}\n"
          f"**Harga Barang:** Rp {data['harga']:,}\n"
          f"**Fee Middleman ({persen:g}%):** Rp {data['fee']:,}\n"
          f"**Total Transfer:** Rp {data['total']:,}"
      ),
      inline=False,
  )
  payment_embed.add_field(
      name="📋 Alur Transaksi",
      value=(
          f"1️⃣ {buyer_mention} (**Buyer**) wajib transfer **Total Transfer** di atas "
          "ke rekening/e-wallet MM resmi (scan QRIS di bawah). Fee middleman sudah "
          "termasuk di nominal ini, Seller tidak perlu transfer apa-apa.\n"
          f"2️⃣ Setelah dana masuk & diamankan oleh MM, MM akan memberi instruksi ke "
          f"{seller_mention} (**Seller**) untuk menyerahkan item ke Buyer.\n"
          "3️⃣ Buyer mengecek item di dalam game & mengonfirmasi ke MM jika item sudah diterima dengan aman.\n"
          "4️⃣ Setelah fix, MM akan meneruskan dana bersih (harga barang) ke pihak Seller."
      ),
      inline=False,
  )
  payment_embed.add_field(
      name="⚠️ Catatan",
      value=(
          "• Jangan melakukan transaksi di luar jalur instruksi MM.\n"
          "• Segala bentuk penipuan di luar arahan MM di luar tanggung jawab MM."
      ),
      inline=False,
  )

  tax_channel = discord.utils.get(guild.text_channels, name=TAX_CHANNEL_NAME)
  tax_channel_ref = tax_channel.mention if tax_channel else f"#{TAX_CHANNEL_NAME}"
  payment_embed.add_field(
      name="🧮 Cek Ulang Perhitungan",
      value=(
          f"Ragu sama hitungan fee di atas? Cek manual di {tax_channel_ref} "
          "atau ketik `/mm` di mana aja buat hitung sendiri (pakai tax default)."
      ),
      inline=False,
  )

  files = []
  if os.path.exists(QRIS_IMAGE_PATH):
    file = discord.File(QRIS_IMAGE_PATH, filename="qris.png")
    payment_embed.set_image(url="attachment://qris.png")
    files.append(file)
  else:
    payment_embed.add_field(
        name="⚠️ QRIS belum di-setup", value="Admin belum upload gambar QRIS.", inline=False
    )

  qris_message = await channel.send(
      embed=payment_embed, files=files, view=ConfirmPaymentView()
  )
  data["qris_message_id"] = qris_message.id


# ---------- Tombol Konfirmasi Pembayaran (khusus role Middleman) ----------
# Klik tombol ini TIDAK langsung buka modal -- cuma kasih pesan konfirmasi
# bahwa dana sudah diterima. Detail item/world (Seller) dan GrowID (Buyer)
# dipisah jadi 2 modal berbeda, masing-masing cuma bisa diisi oleh pihak yang
# bersangkutan.
class ConfirmPaymentView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Konfirmasi Pembayaran",
      style=discord.ButtonStyle.blurple,
      custom_id="confirm_payment_btn",
  )
  async def confirm_payment(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_middleman(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{MIDDLEMAN_ROLE_NAME}` yang bisa konfirmasi pembayaran.",
          ephemeral=True,
      )
      return

    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return
    if data.get("payment_confirmed"):
      await interaction.response.send_message(
          "⚠️ Pembayaran untuk tiket ini sudah dikonfirmasi sebelumnya.",
          ephemeral=True,
      )
      return

    data["payment_confirmed"] = True

    # Matikan tombol Konfirmasi Pembayaran biar gak dobel-klik
    disabled_view = ConfirmPaymentView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    seller = interaction.guild.get_member(data["seller_id"])
    confirm_embed = discord.Embed(
        title="✅ Pembayaran Terkonfirmasi",
        description=(
            f"Dana sudah diterima & diamankan oleh {interaction.user.mention}.\n\n"
            f"{seller.mention if seller else 'Seller'}, silakan klik tombol di bawah "
            "untuk isi Nama Item & World tempat transaksi."
        ),
        color=discord.Color.gold(),
    )
    confirm_msg = await interaction.channel.send(
        embed=confirm_embed, view=SellerDetailView()
    )
    data["confirm_message_id"] = confirm_msg.id


# ---------- Tombol "Isi Detail Transaksi" -- KHUSUS SELLER (item + world) ----------
class SellerDetailView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="📝 Isi Item & World (Seller)",
      style=discord.ButtonStyle.green,
      custom_id="seller_detail_btn",
  )
  async def fill_detail(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["seller_id"]:
      await interaction.response.send_message(
          "❌ Cuma Seller di transaksi ini yang bisa isi bagian ini.",
          ephemeral=True,
      )
      return

    if data.get("seller_detail_filled"):
      await interaction.response.send_message(
          "⚠️ Detail item & world sudah diisi sebelumnya.", ephemeral=True
      )
      return

    await interaction.response.send_modal(SellerDetailModal(data.get("item_name", "")))


class SellerDetailModal(discord.ui.Modal, title="Detail Item & World (Seller)"):

  nama_item = discord.ui.TextInput(label="Nama Item / Barang", max_length=100)
  world = discord.ui.TextInput(
      label="Nama World Growtopia",
      placeholder="Contoh: XYPHERMM",
      max_length=50,
  )

  def __init__(self, default_item_name: str = ""):
    super().__init__()
    if default_item_name:
      self.nama_item.default = default_item_name

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})
    data["item_name"] = self.nama_item.value
    data["world"] = self.world.value
    data["seller_detail_filled"] = True
    TICKET_DATA[channel_id] = data

    buyer = interaction.guild.get_member(data["buyer_id"])

    result_embed = discord.Embed(
        title="🌍 World Transaksi Sudah Diset Seller",
        color=discord.Color.gold(),
    )
    result_embed.add_field(name="📦 Item", value=self.nama_item.value, inline=True)
    result_embed.add_field(
        name="💰 Harga", value=f"Rp {data.get('harga', 0):,}", inline=True
    )
    result_embed.add_field(name="🌍 World", value=self.world.value, inline=False)
    result_embed.set_footer(text="Selanjutnya Buyer wajib isi GrowID di bawah.")

    await interaction.response.send_message(
        content=f"{buyer.mention if buyer else ''}", embed=result_embed
    )

    # Matikan tombol seller biar gak dobel-isi
    seller_msg_id = data.get("confirm_message_id")
    if seller_msg_id:
      try:
        seller_msg = await interaction.channel.fetch_message(seller_msg_id)
        disabled_view = SellerDetailView()
        for child in disabled_view.children:
          child.disabled = True
        await seller_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    # Munculin tombol buat Buyer isi GrowID sendiri
    growid_prompt = await interaction.channel.send(
        content=f"{buyer.mention if buyer else ''}",
        embed=discord.Embed(
            description="🔑 Buyer, silakan klik tombol di bawah untuk isi GrowID kamu "
            "(akun yang bakal nerima item), biar Seller gak salah kasih ke orang lain.",
            color=discord.Color.blue(),
        ),
        view=BuyerGrowIDView(),
    )
    data["seller_detail_message_id"] = growid_prompt.id


# ---------- Tombol "Isi GrowID" -- KHUSUS BUYER ----------
class BuyerGrowIDView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔑 Isi GrowID Saya (Buyer)",
      style=discord.ButtonStyle.green,
      custom_id="buyer_growid_btn",
  )
  async def fill_growid(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di transaksi ini yang bisa isi GrowID.", ephemeral=True
      )
      return

    if data.get("buyer_growid_filled"):
      await interaction.response.send_message(
          "⚠️ GrowID sudah diisi sebelumnya.", ephemeral=True
      )
      return

    await interaction.response.send_modal(BuyerGrowIDModal())


class BuyerGrowIDModal(discord.ui.Modal, title="GrowID Kamu (Buyer)"):

  growid = discord.ui.TextInput(
      label="GrowID Penerima Item",
      placeholder="Contoh: XYPHERSTORE",
      max_length=50,
  )

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})
    data["growid"] = self.growid.value
    data["buyer_growid_filled"] = True
    TICKET_DATA[channel_id] = data

    seller = interaction.guild.get_member(data["seller_id"])

    final_embed = discord.Embed(
        title="✅ Semua Detail Transaksi Lengkap",
        color=discord.Color.gold(),
    )
    final_embed.add_field(name="📦 Item", value=data.get("item_name", "-"), inline=True)
    final_embed.add_field(
        name="💰 Harga", value=f"Rp {data.get('harga', 0):,}", inline=True
    )
    final_embed.add_field(name="🌍 World", value=data.get("world", "-"), inline=False)
    final_embed.add_field(
        name="🧑‍🌾 GrowID Penerima (Buyer)", value=self.growid.value, inline=False
    )
    final_embed.set_footer(
        text=f"{seller.name if seller else 'Seller'}, silakan drop item ke GrowID di atas, di world yang sudah ditentukan."
    )

    await interaction.response.send_message(
        content=f"{seller.mention if seller else ''}", embed=final_embed
    )

    # Matikan tombol isi GrowID biar gak dobel-isi
    msg_id = data.get("seller_detail_message_id")
    if msg_id:
      try:
        msg = await interaction.channel.fetch_message(msg_id)
        disabled_view = BuyerGrowIDView()
        for child in disabled_view.children:
          child.disabled = True
        await msg.edit(view=disabled_view)
      except discord.NotFound:
        pass


# ---------- Tombol Tutup Tiket ----------
class CloseTicketView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔒 Tutup Tiket",
      style=discord.ButtonStyle.red,
      custom_id="close_ticket_btn",
  )
  async def close_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "⚠️ Tiket akan ditutup dan channel ini akan dihapus dalam 3 detik...",
        ephemeral=False,
    )
    TICKET_DATA.pop(interaction.channel.id, None)
    await asyncio.sleep(3)
    await interaction.channel.delete()


# ---------- Tombol Utama Pembuat Tiket ----------
class TicketButton(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🛒 Order Middleman (Buat Tiket)",
      style=discord.ButtonStyle.green,
      custom_id="create_ticket_btn",
  )
  async def create_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "👥 Pilih lawan transaksi kamu & tentukan siapa Buyer di bawah ini.\n\n"
        "⚠️ **Pastikan lawan transaksi sudah join server ini dulu**, "
        "biar bisa muncul di list & langsung diundang ke channel.",
        view=SetupView(interaction.user),
        ephemeral=True,
    )


@bot.event
async def on_ready():
  print(f"Bot {bot.user.name} berhasil online dan siap bertugas!")
  bot.add_view(TicketButton())
  bot.add_view(CloseTicketView())
  bot.add_view(TaxPromptView())
  bot.add_view(ConfirmPaymentView())
  bot.add_view(SellerDetailView())
  bot.add_view(BuyerGrowIDView())


@bot.tree.command(
    name="mm",
    description="Hitung fee middleman & total transfer dari harga barang (pakai tax default)",
)
@app_commands.describe(
    harga="Harga barang, angka saja (contoh: 50000)",
    persen="Opsional: persentase tax custom (default pakai MM_FEE_PERCENT)",
)
async def mm_calculator(
    interaction: discord.Interaction, harga: int, persen: float | None = None
):
  if harga <= 0:
    await interaction.response.send_message(
        "❌ Harga harus lebih dari 0.", ephemeral=True
    )
    return

  pakai_persen = persen if persen is not None else MM_FEE_PERCENT
  fee, total = hitung_fee(harga, pakai_persen)
  embed = discord.Embed(
      title="🧮 Kalkulator Fee Middleman",
      color=discord.Color.blue(),
  )
  embed.add_field(name="Harga Barang", value=f"Rp {harga:,}", inline=True)
  embed.add_field(name=f"Fee ({pakai_persen:g}%)", value=f"Rp {fee:,}", inline=True)
  embed.add_field(name="Total Transfer", value=f"Rp {total:,}", inline=False)
  embed.set_footer(
      text="Ini cuma kalkulator manual/referensi -- tax beneran di tiap "
      "tiket tetap diisi Middleman lewat tombol 'Set Tax'."
  )

  await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup_ticket(ctx):
  embed = discord.Embed(
      title="🛡️ XypherStore Middleman Service",
      description=(
          "Mau pakai jasa Middleman (MM)?\nKlik tombol di bawah untuk membuat"
          " **Private Channel** transaksi secara otomatis!"
      ),
      color=discord.Color.blue(),
  )
  embed.set_footer(text="XypherStore Automated System")
  await ctx.send(embed=embed, view=TicketButton())
  await ctx.message.delete()


bot.run(TOKEN)